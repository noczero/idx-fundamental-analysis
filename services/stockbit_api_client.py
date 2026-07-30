import os
import tempfile
import time

import requests

from utils.logger_config import logger
from services.stockbit_token_fetcher import StockbitTokenFetcher


class StockbitReauthRequiredError(RuntimeError):
    """
    Raised when Stockbit authentication cannot be recovered without a local,
    interactive re-bootstrap — i.e. the refresh token is dead/expired AND browser
    login is disabled (STOCKBIT_DISABLE_BROWSER_LOGIN, the headless-server mode).

    It is a hard-stop signal: retrying against every request is pointless, so the
    caller should abort the run instead of logging the same error thousands of
    times.
    """


class StockbitApiClient:
    """
    Handles HTTP requests to the Stockbit API, including authentication and retries.
    """

    def __init__(self, auto_authenticate: bool = True):
        """
        Initializes the StockbitHttpRequest with a URL and optional headers.
        Authenticates with the Stockbit API upon initialization.

        Parameters:
        - auto_authenticate (bool): When True (default), validate/renew the token
          on construction. Set False for the interactive bootstrap so constructing
          the client does not trigger a login before ``bootstrap_login`` runs.
        """
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:137.0) Gecko/20100101 Firefox/137.0",
        }

        self.auto_authenticate = auto_authenticate

        self.is_authorise = False

        # Where token files live. Defaults to the system temp dir, but on a
        # server /tmp can be wiped on reboot, so allow a persistent, explicit
        # location via STOCKBIT_TOKEN_DIR.
        token_dir = os.environ.get("STOCKBIT_TOKEN_DIR") or tempfile.gettempdir()
        os.makedirs(token_dir, exist_ok=True)
        self.token_dir = token_dir

        # On a headless server (no Chrome) the browser login can never succeed.
        # Setting STOCKBIT_DISABLE_BROWSER_LOGIN makes the client rely purely on
        # the refresh token and fail with a clear, actionable message instead of
        # trying (and failing) to launch a browser.
        self.disable_browser_login = os.environ.get(
            "STOCKBIT_DISABLE_BROWSER_LOGIN", ""
        ).strip().lower() in ("1", "true", "yes")

        self.token_temp_file_path = os.path.join(token_dir, "stockbit_token.tmp")

        self.refresh_token_temp_file_path = os.path.join(
            token_dir, "stockbit_refresh_token.tmp"
        )

        self.ua_temp_file_path = os.path.join(token_dir, "stockbit_ua.tmp")

        self._initialize_token_file()

    def _request(self, url: str, method: str, payload: dict = None):
        """
        Makes an HTTP request with the specified method and payload, retrying on failure.

        Parameters:
        - method (str): The HTTP method ("GET" or "POST").
        - payload (dict): Optional payload for POST requests.

        Returns:
        - dict: The JSON response from the server, or an empty dictionary on failure.
        """
        retry = 0
        while retry <= 3:
            try:
                if method == "GET":
                    response = requests.get(url, headers=self.headers)
                elif method == "POST":
                    response = requests.post(url, headers=self.headers, json=payload)
                else:
                    raise ValueError("Unsupported HTTP method")

                logger.debug(url)
                logger.debug(response.status_code)
                logger.debug(response.json())

                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(
                        f"Error: Received status code {response.status_code}, "
                        f"text: {response.text}, "
                        f"retry: {retry}"
                    )
                    if response.status_code == 401:
                        self._authenticate_stockbit()
                        retry += 1
                    else:
                        break

            except requests.exceptions.RequestException as e:
                logger.error(f"Request failed: {e} retry: {retry}")
                break

            time.sleep(0.2)

        logger.error(f"Failed to retrieve key statistics retry: {retry}")
        return {}

    def get(self, url: str):
        """
        Performs a GET request using the stored URL and headers.

        Returns:
        - dict: The JSON response from the server, or an empty dictionary on failure.
        """
        return self._request(url, "GET")

    def post(self, url: str, payload: dict):
        """
        Performs a POST request using the stored URL, headers, and provided payload.

        Parameters:
        - payload (dict): The payload for the POST request.

        Returns:
        - dict: The JSON response from the server, or an empty dictionary on failure.
        """
        return self._request(url, "POST", payload)

    def bootstrap_login(self) -> bool:
        """
        Run the interactive browser login and persist the access token, refresh
        token and User-Agent to ``self.token_dir``.

        Intended for the one-time local bootstrap: run this on a machine with
        Chrome, then sync the resulting token files to the (browser-less) server.

        Returns:
            bool: True if a token was obtained, False otherwise.
        """
        self._login()
        return self.is_authorise

    def _authenticate_stockbit(self):
        """
        Authenticates with the Stockbit API and updates the authorization header.

        Prefer the browser-free refresh path whenever a refresh token is
        available (even on a fresh process); only fall back to an interactive
        browser login when no refresh token exists.
        """

        if not self._is_refresh_token_empty():
            self._refresh_token()
        else:
            self._login()

    def _login(self):
        """
        Login to Stockbit API via an interactive browser session.

        Requires a machine with Chrome. On headless servers this is disabled via
        STOCKBIT_DISABLE_BROWSER_LOGIN; there, an expired/invalid refresh token
        is a hard error that requires re-running the local bootstrap.
        """
        if self.disable_browser_login:
            self.is_authorise = False
            # Hard stop: on a headless server there is no way to recover, so abort
            # the whole run rather than repeat this for every request.
            raise StockbitReauthRequiredError(
                "Stockbit requires a local re-bootstrap: the refresh token is "
                "invalid/expired and browser login is disabled "
                "(STOCKBIT_DISABLE_BROWSER_LOGIN). Re-run "
                "`uv run python main.py --stockbit-login` on a machine with a "
                f"browser, then sync the token files in {self.token_dir} to this host."
            )

        self.headers["Authorization"] = None

        token = None
        refresh_token = None
        user_agent = None

        fetcher = None
        try:
            fetcher = StockbitTokenFetcher()
            token, refresh_token, user_agent = fetcher.fetch_tokens()
        except Exception as e:
            logger.error(f"Failed to fetch tokens via StockbitTokenFetcher: {e}")
        finally:
            if fetcher is not None:
                try:
                    fetcher.close()
                except Exception:
                    pass

        if token:
            logger.info("Logged in successfully via StockbitTokenFetcher!")
            self.headers["Authorization"] = f"Bearer {token}"

            if user_agent:
                self.headers["User-Agent"] = user_agent
                logger.info(f"Updated User-Agent to: {user_agent}")

            self._write_token(token, refresh_token or "", user_agent)
            self.is_authorise = True
        else:
            logger.error("Failed to log in via StockbitTokenFetcher.")
            self.is_authorise = False

        time.sleep(1)

    def _refresh_token(self):
        """
        Renew the access token using the stored refresh token.

        Outcomes are handled differently so a dead refresh token surfaces a
        clear, actionable error instead of silently launching a browser login:

        * 200          -> success; rotate and persist both tokens.
        * 401 / 403    -> the refresh token itself is rejected. Stockbit revokes
                          and rotates refresh tokens server-side, so a token can
                          be dead even though its JWT ``exp`` is still in the
                          future. Discard it (so we don't keep retrying a token
                          that can never work) and re-authenticate via ``_login``
                          (which honours STOCKBIT_DISABLE_BROWSER_LOGIN).
        * other status -> treated as transient; keep the refresh token so a
                          later retry can use it.
        * network error-> transient; keep the refresh token.
        """
        url = "https://exodus.stockbit.com/login/refresh"

        try:
            with open(self.refresh_token_temp_file_path, "r") as file:
                refresh_token = file.read().strip()
        except FileNotFoundError:
            refresh_token = ""

        if not refresh_token:
            # Nothing to refresh with; fall back to (possibly disabled) login.
            self._login()
            return

        self.headers["Authorization"] = f"Bearer {refresh_token}"

        try:
            response = requests.post(url, headers=self.headers)
        except requests.exceptions.RequestException as e:
            logger.error(f"Token refresh request failed (transient): {e}")
            self.is_authorise = False
            return

        if response.status_code == 200:
            try:
                data = response.json()["data"]
                token = data["access"]["token"]
                new_refresh_token = data["refresh"]["token"]
            except (KeyError, ValueError) as e:
                logger.error(
                    "Token refresh returned 200 but the response was malformed "
                    f"({e}). Body: {response.text[:300]}"
                )
                self.is_authorise = False
                return

            logger.info("Token is successfully refreshed!")
            self.headers["Authorization"] = f"Bearer {token}"
            self._write_token(token, new_refresh_token)
            self.is_authorise = True
            time.sleep(1)
            return

        if response.status_code in (401, 403):
            # Dead refresh token: rejected server-side regardless of its JWT exp.
            logger.warning(
                f"Stockbit rejected the refresh token (status "
                f"{response.status_code}: {response.text[:200]}). It is invalid "
                "or revoked server-side despite its JWT expiry; re-authenticating."
            )
            self._clear_refresh_token()
            self._login()
            return

        # Anything else (5xx, rate limiting, ...) is likely transient: keep the
        # refresh token untouched so the next attempt can reuse it.
        logger.error(
            f"Unexpected status refreshing token: {response.status_code} - "
            f"{response.text[:200]}. Keeping refresh token for a later retry."
        )
        self.is_authorise = False

    def _write_token(self, token, refresh_token, user_agent=None):
        """
        Persist the tokens.

        The refresh token is written BEFORE the access token on purpose.
        Stockbit rotates (single-use) the refresh token on every successful
        refresh, so a fresh access token paired with a stale/consumed refresh
        token is the exact state that forces an interactive login on the next
        run. Writing refresh-first means an interruption mid-write leaves
        (new refresh + old access), which self-heals on the next refresh, rather
        than (new access + dead refresh), which does not.

        Each file is written atomically (temp file + ``os.replace``) so a reader
        never observes a half-written or empty token file.

        :param token: access token
        :param refresh_token: refresh token (may be "")
        :param user_agent: optional User-Agent to persist alongside the tokens
        :return:
        """
        self._atomic_write(self.refresh_token_temp_file_path, refresh_token)
        self._atomic_write(self.token_temp_file_path, token)

        if user_agent:
            self._atomic_write(self.ua_temp_file_path, user_agent)

    @staticmethod
    def _atomic_write(path, content):
        """
        Write ``content`` to ``path`` atomically: write to a per-process temp
        file, fsync it, then rename over the destination. Guarantees the target
        is always either the complete old or complete new content.
        """
        tmp_path = f"{path}.{os.getpid()}.part"
        with open(tmp_path, "w") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, path)

    def _clear_refresh_token(self):
        """Blank out a dead refresh token so it is not retried on later runs."""
        try:
            self._atomic_write(self.refresh_token_temp_file_path, "")
        except OSError as e:
            logger.error(f"Failed to clear the refresh token file: {e}")

    def _initialize_token_file(self):
        """
        Intialize token files
        :return:
        """
        try:
            with open(self.refresh_token_temp_file_path, "r") as file:
                file.read()
        except FileNotFoundError:
            with open(self.refresh_token_temp_file_path, "w") as file:
                file.write("")

        try:
            with open(self.ua_temp_file_path, "r") as file:
                ua = file.read()
                if ua != "":
                    self.headers["User-Agent"] = ua
        except FileNotFoundError:
            pass

        try:
            with open(self.token_temp_file_path, "r") as file:
                token = file.read()
                logger.debug(f"Token: {token}")
                if token != "":
                    self.headers["Authorization"] = f"Bearer {token}"

                if self.auto_authenticate:
                    self._request_challenge()
        except FileNotFoundError:
            with open(self.token_temp_file_path, "w") as file:
                file.write("")

    def _is_refresh_token_empty(self) -> bool:
        """
        Check if token is empty.
        :return: boolean
        """
        try:
            with open(self.refresh_token_temp_file_path, "r") as file:
                token = file.read()
                return token.strip() == ""
        except FileNotFoundError:
            # No file means there is no refresh token to use.
            return True

    def _request_challenge(self):
        """
        Check expired token by request to light API
        :return:
        """
        try:
            response = requests.get(
                "https://exodus.stockbit.com/research/indicator/new",
                headers=self.headers,
            )

            if response.status_code != 200:
                logger.error(
                    f"Error: Received status code {response.status_code} - {response.text}"
                )
                self._authenticate_stockbit()
            else:
                logger.info("Logged in successfully with existing token!")

        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
