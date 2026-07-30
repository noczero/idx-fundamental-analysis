import base64
import json
import logging
import os
import re
import subprocess
import tempfile

import undetected_chromedriver as uc
from utils.logger_config import logger

# A JWT is three base64url segments separated by dots; Stockbit access and
# refresh tokens both start with "eyJ" (base64 of '{"').
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

# Stockbit endpoints that carry the *refresh* token in their Authorization header.
_REFRESH_ENDPOINT_HINT = "login/refresh"


def _decode_jwt_claims(token):
    """Return the decoded JWT payload dict, or None if it cannot be decoded."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def _jwt_lifetime(token):
    """Return the token lifetime (exp - iat) in seconds, or -1 if unknown."""
    claims = _decode_jwt_claims(token) or {}
    if "exp" in claims and "iat" in claims:
        try:
            return int(claims["exp"]) - int(claims["iat"])
        except (TypeError, ValueError):
            return -1
    return -1


def _detect_chrome_major_version():
    """
    Return the major version of the locally installed Chrome, or None if it
    cannot be determined.

    undetected-chromedriver otherwise downloads the *latest* ChromeDriver, which
    fails with SessionNotCreatedException when it does not match the installed
    Chrome (e.g. driver for Chrome 151 vs. an installed Chrome 150).
    """
    try:
        chrome_path = uc.find_chrome_executable()
        if not chrome_path:
            return None
        output = subprocess.check_output(
            [chrome_path, "--version"],
            text=True,
            stderr=subprocess.STDOUT,
        )
        match = re.search(r"(\d+)\.\d+\.\d+", output)
        if match:
            return int(match.group(1))
    except Exception:
        return None
    return None

# Suppress noisy logs
for _name in (
    "selenium",
    "selenium.webdriver",
    "selenium.webdriver.remote.remote_connection",
    "urllib3",
):
    logging.getLogger(_name).setLevel(logging.WARNING)


class StockbitTokenFetcher:
    def __init__(self):
        self.login_url = "https://stockbit.com/login"
        self.sample_url = "exodus.stockbit.com/chat/v2/rooms/unread/count"

        profile_dir = os.path.join(
            os.path.expanduser("~"), ".idx-fundamental-stockbit-profile"
        )
        os.makedirs(profile_dir, exist_ok=True)

        options = uc.ChromeOptions()
        options.add_argument(f"--user-data-dir={profile_dir}")
        # options.add_argument(f"--disk-cache-dir={cache_dir}") # UC handles profile better without explicit cache dir split sometimes, but keeping user-data-dir is key.

        # Enable performance logging to capture headers
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        # Initialize undetected-chromedriver
        # headless=False is important for manual login
        # Pin the driver to the installed Chrome major version so uc downloads a
        # matching ChromeDriver instead of the latest, avoiding
        # SessionNotCreatedException on version mismatch.
        chrome_version = _detect_chrome_major_version()
        if chrome_version is not None:
            logger.info(f"Detected installed Chrome major version: {chrome_version}")

        # uc.Chrome() blocks while it downloads/patches a matching ChromeDriver
        # and launches the browser; on a first run this can take tens of seconds
        # with no output, which looks like a hang. Log before and after so it is
        # clear the process is working, not stuck.
        logger.info(
            "Launching Chrome for interactive login (first run may take a while "
            "to download the matching ChromeDriver)..."
        )
        self.driver = uc.Chrome(
            options=options,
            headless=False,
            use_subprocess=True,
            version_main=chrome_version,
        )
        logger.info("Chrome launched. A browser window should now be open.")

        # Keep the fetcher's own token dump in the same place StockbitApiClient
        # reads/writes tokens, so we don't leave a stray copy in the system temp
        # dir when STOCKBIT_TOKEN_DIR points elsewhere (e.g. a persistent path
        # on a server where /tmp is wiped on reboot).
        tmp_dir = os.environ.get("STOCKBIT_TOKEN_DIR") or tempfile.gettempdir()
        os.makedirs(tmp_dir, exist_ok=True)
        self.token_path = os.path.join(tmp_dir, "stockbit_token.tmp")

    def fetch_tokens(self):
        driver = self.driver
        logger.info("Navigating to Stockbit login page...")
        driver.get(self.login_url)

        logger.info("Please log in to Stockbit in the opened browser.")
        input("Press Enter here AFTER login succeeds and the dashboard loads... ")

        # Scan performance logs for the sample request
        logs = driver.get_log("performance")

        access_token = None
        refresh_from_network = None

        # We look for Network.requestWillBeSent events
        for entry in logs:
            try:
                message = json.loads(entry["message"])
                method = message.get("message", {}).get("method")
                if method == "Network.requestWillBeSent":
                    params = message["message"]["params"]
                    request = params.get("request", {})
                    url = request.get("url", "")

                    headers = request.get("headers", {})
                    # Headers keys can be case-sensitive or not depending on browser version, usually title-cased or lowercase.
                    # We check both.
                    auth_header = headers.get("Authorization") or headers.get(
                        "authorization"
                    )

                    if not (auth_header and auth_header.startswith("Bearer ")):
                        continue

                    bearer = auth_header.split(" ", 1)[1]

                    if self.sample_url in url:
                        access_token = bearer
                        # Don't break, keep looking for the LATEST token in the logs
                    elif _REFRESH_ENDPOINT_HINT in url:
                        # A call to login/refresh carries the refresh token itself.
                        refresh_from_network = bearer
            except (KeyError, json.JSONDecodeError):
                continue

        if not access_token:
            logger.error(
                "Could not find Bearer token in captured requests. Make sure the page finished loading."
            )
            return None, None, None

        # Capture the User-Agent used by the browser
        user_agent = driver.execute_script("return navigator.userAgent;")
        logger.info(f"User-Agent captured: {user_agent}")

        logger.info("Access token captured.")

        refresh_token = self._extract_refresh_token(driver, access_token, refresh_from_network)
        if refresh_token:
            logger.info(
                f"Refresh token captured (lifetime ~{_jwt_lifetime(refresh_token) // 3600}h)."
            )
        else:
            logger.warning(
                "No refresh token found in browser storage. The server will not be able "
                "to renew the token on its own and will need periodic re-bootstrap."
            )

        with open(self.token_path, "w") as f:
            f.write(access_token)

        logger.info(f"Tokens written to: {self.token_path}")

        return access_token, refresh_token, user_agent

    def _extract_refresh_token(self, driver, access_token, refresh_from_network=None):
        """
        Locate the Stockbit refresh token so the server can renew access tokens
        without a browser.

        Priority:
          1. A refresh token seen in a login/refresh request's Authorization header.
          2. A JWT persisted in localStorage/cookies that is not the access token.
             The refresh token outlives the 24h access token, so among candidates
             we pick the one with the longest lifetime.
        """
        if refresh_from_network and refresh_from_network != access_token:
            return refresh_from_network

        candidates = {}  # token -> source label
        try:
            local_storage = (
                driver.execute_script(
                    "var o={};"
                    "for(var i=0;i<window.localStorage.length;i++)"
                    "{var k=window.localStorage.key(i);o[k]=window.localStorage.getItem(k);}"
                    "return o;"
                )
                or {}
            )
        except Exception:
            local_storage = {}

        if local_storage:
            logger.debug(f"localStorage keys: {list(local_storage.keys())}")

        for key, value in local_storage.items():
            if not isinstance(value, str):
                continue
            for match in _JWT_RE.findall(value):
                candidates.setdefault(match, f"localStorage[{key}]")

        try:
            cookies = driver.get_cookies()
        except Exception:
            cookies = []
        for cookie in cookies:
            for match in _JWT_RE.findall(cookie.get("value", "") or ""):
                candidates.setdefault(match, f"cookie[{cookie.get('name')}]")

        best = None  # (lifetime, token, source)
        for token, source in candidates.items():
            if token == access_token:
                continue
            lifetime = _jwt_lifetime(token)
            if best is None or lifetime > best[0]:
                best = (lifetime, token, source)

        if best is not None:
            logger.info(f"Refresh token source: {best[2]}")
            return best[1]
        return None

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass
