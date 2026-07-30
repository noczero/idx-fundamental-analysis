import base64
import json
import os
import re
import tempfile

from camoufox.sync_api import Camoufox
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from utils.logger_config import logger

# A JWT is three base64url segments separated by dots; Stockbit access and
# refresh tokens both start with "eyJ" (base64 of '{"').
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

# Host that serves Stockbit's authenticated API. Every logged-in API call carries
# the *access* token in its Authorization header, so matching on the host (rather
# than one hardcoded path) is robust to Stockbit changing individual endpoints.
_API_HOST_HINT = "exodus.stockbit.com"

# Endpoint that carries the *refresh* token in its Authorization header.
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


def _looks_like_stockbit_jwt(claims):
    """True if the decoded JWT claims look like a Stockbit-issued token."""
    if (claims or {}).get("iss") == "STOCKBIT":
        return True
    data = (claims or {}).get("data") or {}
    return "uid" in data


def _is_refresh_claims(claims):
    """True if the decoded JWT is a Stockbit *refresh* token."""
    return ((claims or {}).get("data") or {}).get("typ") == "refresh"


# JS that snapshots the whole of window.localStorage into a plain object. Used
# to find tokens Stockbit persisted client-side.
_LOCAL_STORAGE_SNAPSHOT_JS = """
() => {
  const o = {};
  for (let i = 0; i < window.localStorage.length; i++) {
    const k = window.localStorage.key(i);
    o[k] = window.localStorage.getItem(k);
  }
  return o;
}
"""


class StockbitTokenFetcher:
    """
    Interactive Stockbit login driven by Camoufox (a stealth Firefox fork on top
    of Playwright), used to capture the access token, refresh token and browser
    User-Agent for later browser-free renewal on a server.

    Camoufox replaces the previous undetected-chromedriver/Selenium stack: the
    Firefox-based, anti-fingerprinting engine is far less likely to be blocked,
    and there is no Chrome/ChromeDriver version matching to worry about.

    Tokens are captured from two independent sources for robustness:
      1. The Authorization header of authenticated API calls (network capture).
      2. Client-side storage (localStorage/cookies) read after login.
    """

    def __init__(self):
        self.login_url = "https://stockbit.com/login"

        # Persistent Camoufox (Firefox) profile so a prior login is remembered
        # across runs. Kept separate from the old Chrome profile dir since the
        # on-disk profile formats differ.
        self.profile_dir = os.path.join(
            os.path.expanduser("~"), ".idx-fundamental-stockbit-camoufox"
        )
        os.makedirs(self.profile_dir, exist_ok=True)

        # Keep the fetcher's own token dump in the same place StockbitApiClient
        # reads/writes tokens, so we don't leave a stray copy in the system temp
        # dir when STOCKBIT_TOKEN_DIR points elsewhere (e.g. a persistent path
        # on a server where /tmp is wiped on reboot).
        tmp_dir = os.environ.get("STOCKBIT_TOKEN_DIR") or tempfile.gettempdir()
        os.makedirs(tmp_dir, exist_ok=True)
        self.token_path = os.path.join(tmp_dir, "stockbit_token.tmp")

    def fetch_tokens(self):
        """
        Open a real browser, let the user log in to Stockbit, and capture the
        access token, refresh token and User-Agent.

        Returns:
            (access_token, refresh_token, user_agent), any of which may be None
            if capture failed.
        """
        # Latest Bearer tokens seen on the wire, updated by the request handler.
        captured = {"access": None, "refresh_from_network": None}

        def on_request(request):
            # Playwright lower-cases header names.
            auth_header = request.headers.get("authorization")
            if not (auth_header and auth_header.startswith("Bearer ")):
                return
            bearer = auth_header.split(" ", 1)[1]
            url = request.url
            if _REFRESH_ENDPOINT_HINT in url:
                # A call to login/refresh carries the refresh token itself.
                captured["refresh_from_network"] = bearer
            elif _API_HOST_HINT in url:
                # Any authenticated API call carries the access token. Keep the
                # LATEST one seen.
                captured["access"] = bearer

        logger.info(
            "Launching Camoufox (stealth Firefox) for interactive login; the "
            "first run downloads the browser and may take a while..."
        )

        # persistent_context=True yields a Playwright BrowserContext (not a
        # Browser) backed by the on-disk profile.
        with Camoufox(
            headless=False,
            persistent_context=True,
            user_data_dir=self.profile_dir,
            humanize=True,
            geoip=True,
        ) as context:
            # Catch requests from every page/popup in the context.
            context.on("request", on_request)

            page = context.pages[0] if context.pages else context.new_page()

            logger.info("Navigating to Stockbit login page...")
            page.goto(self.login_url, wait_until="domcontentloaded")

            logger.info("A browser window is open. Please log in to Stockbit.")
            input("Press Enter here AFTER login succeeds and the dashboard loads... ")

            # Playwright's sync event loop is NOT pumped while we block on
            # input(), so Bearer tokens sent during login were likely never
            # dispatched to on_request. Reload the dashboard to re-issue
            # authenticated API calls while we actively drive Playwright (which
            # dispatches the request events), then also read tokens straight from
            # client-side storage as a fallback.
            logger.info("Reloading to capture the authenticated session token...")
            try:
                # wait_until="commit" resolves as soon as the navigation response
                # starts, so a dashboard with long-lived/streaming requests can't
                # hang the reload (unlike "load"/"networkidle"). The subsequent
                # wait_for_timeout pumps the event loop while deferred XHRs fire
                # and are captured by on_request.
                page.reload(wait_until="commit", timeout=8000)
            except PlaywrightTimeoutError:
                logger.warning("Reload timed out; relying on stored tokens.")
            except Exception as e:
                logger.warning(
                    f"Reload after login failed ({e}); relying on stored tokens."
                )
            try:
                page.wait_for_timeout(2500)
            except Exception:
                pass

            storage_access, storage_refresh = self._extract_tokens_from_storage(
                page, context
            )

            access_token = captured["access"] or storage_access
            refresh_token = captured["refresh_from_network"] or storage_refresh

            # Capture the User-Agent the browser actually used, so the server
            # sends the same UA alongside the token.
            user_agent = page.evaluate("() => navigator.userAgent")

        if not access_token:
            logger.error(
                "Could not capture an access token from the network or from "
                "browser storage. Make sure the dashboard fully loaded (you are "
                "logged in) before pressing Enter."
            )
            return None, None, None

        logger.info(f"User-Agent captured: {user_agent}")
        logger.info(
            "Access token captured "
            f"(via {'network' if captured['access'] else 'storage'})."
        )

        if refresh_token and refresh_token != access_token:
            logger.info(
                f"Refresh token captured (lifetime ~{_jwt_lifetime(refresh_token) // 3600}h)."
            )
        else:
            refresh_token = None
            logger.warning(
                "No refresh token found. The server will not be able to renew "
                "the token on its own and will need periodic re-bootstrap."
            )

        with open(self.token_path, "w") as f:
            f.write(access_token)
        logger.info(f"Tokens written to: {self.token_path}")

        return access_token, refresh_token, user_agent

    def _collect_jwt_candidates(self, page, context):
        """Collect every JWT found in localStorage and cookies: token -> source."""
        candidates = {}

        try:
            local_storage = page.evaluate(_LOCAL_STORAGE_SNAPSHOT_JS) or {}
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
            cookies = context.cookies()
        except Exception:
            cookies = []
        for cookie in cookies:
            for match in _JWT_RE.findall(cookie.get("value", "") or ""):
                candidates.setdefault(match, f"cookie[{cookie.get('name')}]")

        return candidates

    def _extract_tokens_from_storage(self, page, context):
        """
        Read the Stockbit access and refresh tokens directly from client-side
        storage (localStorage/cookies).

        Among the JWTs found:
          * the refresh token is the one flagged ``typ == "refresh"`` (or, failing
            that, the longest-lived JWT) — it outlives the 24h access token;
          * the access token is the freshest non-refresh Stockbit JWT.
        Freshness is decided by ``iat`` so a stale token left in storage does not
        win over the one just issued at login.

        Returns:
            (access_token, refresh_token), either of which may be None.
        """
        candidates = self._collect_jwt_candidates(page, context)

        best_access = None  # (iat, token, source)
        best_refresh = None  # (iat, token, source)
        for token, source in candidates.items():
            claims = _decode_jwt_claims(token) or {}
            if not _looks_like_stockbit_jwt(claims):
                continue
            iat = claims.get("iat", -1)
            try:
                iat = int(iat)
            except (TypeError, ValueError):
                iat = -1
            if _is_refresh_claims(claims):
                if best_refresh is None or iat > best_refresh[0]:
                    best_refresh = (iat, token, source)
            else:
                if best_access is None or iat > best_access[0]:
                    best_access = (iat, token, source)

        access = best_access[1] if best_access else None
        refresh = best_refresh[1] if best_refresh else None

        if best_access:
            logger.info(f"Access token source (storage): {best_access[2]}")
        if best_refresh:
            logger.info(f"Refresh token source (storage): {best_refresh[2]}")

        # Fallback: no token was explicitly flagged as a refresh token, so use
        # the longest-lived JWT that is not the access token.
        if refresh is None:
            longest = None  # (lifetime, token)
            for token in candidates:
                if token == access:
                    continue
                lifetime = _jwt_lifetime(token)
                if longest is None or lifetime > longest[0]:
                    longest = (lifetime, token)
            if longest is not None:
                refresh = longest[1]

        return access, refresh

    def close(self):
        """
        Kept for API compatibility with the previous Selenium-based fetcher.
        The browser lifetime is now scoped to the ``with Camoufox(...)`` block in
        ``fetch_tokens``, so there is nothing to tear down here.
        """
        return None
