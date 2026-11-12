"""HTTP client for the HPE Aruba New Central (GreenLake) REST API.

No Splunk imports here so that the module can be unit tested standalone.
"""

import email.utils
import logging
import random
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, Iterator, Optional

import requests

USER_AGENT = "TA_hpe_aruba_central/1.0"
TOKEN_REFRESH_MARGIN_S = 300
MAX_RETRY_AFTER_S = 120
MAX_PAGES = 10000


class ArubaCentralError(Exception):
    """Base class for all client errors."""


class ArubaCentralAuthError(ArubaCentralError):
    """Token request rejected or API returned 401 after a token refresh."""


class ArubaCentralConnectionError(ArubaCentralError):
    """DNS, TCP, TLS, proxy or timeout problem."""


class ArubaCentralApiError(ArubaCentralError):
    def __init__(self, message: str, status: Optional[int] = None, path: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.path = path


class RateLimiter:
    """Spaces requests so that at most `rate` requests per second are sent."""

    def __init__(self, rate: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


class TokenStore:
    """Persistence interface for the access token. Default: in-memory only."""

    def load(self) -> Optional[Dict[str, Any]]:
        return None

    def save(self, token: Dict[str, Any]) -> None:
        pass


def _retry_after_seconds(value: Optional[str], now: float) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return max(0.0, parsed.timestamp() - now)


def _error_detail(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:300]
    if isinstance(body, dict):
        for key in ("error_description", "message", "detail", "error", "errorCode"):
            if body.get(key):
                return str(body[key])[:300]
    return str(body)[:300]


class ArubaCentralClient:
    def __init__(
        self,
        base_url: str,
        token_url: str,
        client_id: str,
        client_secret: str,
        max_rps: float = 2,
        timeout: float = 60,
        proxies: Optional[Dict[str, str]] = None,
        token_store: Optional[TokenStore] = None,
        logger: Optional[logging.Logger] = None,
        session: Optional[requests.Session] = None,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._token_store = token_store or TokenStore()
        self._logger = logger or logging.getLogger(__name__)
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if proxies:
            self._session.proxies.update(proxies)
            self._session.trust_env = False
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._limiter = RateLimiter(max_rps, sleep=sleep)
        self._token: Optional[Dict[str, Any]] = None
        self.request_count = 0

    # ------------------------------------------------------------------ token
    def _token_valid(self, token: Optional[Dict[str, Any]]) -> bool:
        return bool(
            token
            and token.get("access_token")
            and float(token.get("expires_at", 0)) - self._clock() > TOKEN_REFRESH_MARGIN_S
        )

    def _fetch_token(self) -> Dict[str, Any]:
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        started = self._clock()
        token_path = urllib.parse.urlparse(self.token_url).path or "/"
        try:
            response = self._session.post(
                self.token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self._timeout,
            )
        except requests.exceptions.RequestException as exc:
            self._log_request("POST", token_path, type(exc).__name__, started, 0)
            raise ArubaCentralConnectionError(
                "Token request to %s failed: %s" % (self.token_url, type(exc).__name__)
            ) from exc
        self._log_request("POST", token_path, response.status_code, started, 0)
        if response.status_code in (400, 401, 403):
            raise ArubaCentralAuthError(
                "Token request rejected (HTTP %s): %s" % (response.status_code, _error_detail(response))
            )
        if response.status_code != 200:
            raise ArubaCentralApiError(
                "Token request failed (HTTP %s): %s" % (response.status_code, _error_detail(response)),
                status=response.status_code,
                path=self.token_url,
            )
        try:
            payload = response.json()
            access_token = payload["access_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ArubaCentralAuthError("Token response did not contain an access_token") from exc
        expires_in = float(payload.get("expires_in") or 7200)
        self._logger.info("action=token_refreshed expires_in=%d", int(expires_in))
        return {"access_token": access_token, "expires_at": self._clock() + expires_in}

    def access_token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            if self._token_valid(self._token):
                return self._token["access_token"]
            stored = None
            try:
                stored = self._token_store.load()
            except Exception as exc:  # cache problems must never stop collection
                self._logger.warning("action=token_cache_load_failed error=%s", type(exc).__name__)
            if self._token_valid(stored):
                self._token = stored
                return stored["access_token"]
        self._token = self._fetch_token()
        try:
            self._token_store.save(self._token)
        except Exception as exc:
            self._logger.warning("action=token_cache_save_failed error=%s", type(exc).__name__)
        return self._token["access_token"]

    # ---------------------------------------------------------------- request
    def _backoff(self, attempt: int) -> float:
        return min(60.0, 2.0 ** attempt) + random.uniform(0, 0.5)

    def _log_request(self, method, path, status, started, attempt):
        """One debug line per HTTP call. Never includes the token, the secret or the body."""
        duration_ms = int(max(0.0, self._clock() - started) * 1000)
        self._logger.debug(
            "action=request method=%s path=%s status=%s duration_ms=%d attempt=%d",
            method, path, status, duration_ms, attempt + 1,
        )

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url + path
        refreshed = False
        attempt = 0
        while True:
            token = self.access_token()
            self._limiter.acquire()
            self.request_count += 1
            started = self._clock()
            try:
                response = self._session.get(
                    url,
                    params=params,
                    headers={"Authorization": "Bearer %s" % token},
                    timeout=self._timeout,
                )
            except requests.exceptions.RequestException as exc:
                self._log_request("GET", path, type(exc).__name__, started, attempt)
                if attempt >= self._max_retries:
                    raise ArubaCentralConnectionError(
                        "GET %s failed after %d attempts: %s" % (path, attempt + 1, type(exc).__name__)
                    ) from exc
                wait = self._backoff(attempt)
                self._logger.warning("action=retry reason=%s path=%s wait_s=%.1f",
                                     type(exc).__name__, path, wait)
                self._sleep(wait)
                attempt += 1
                continue

            status = response.status_code
            self._log_request("GET", path, status, started, attempt)
            if status == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ArubaCentralApiError("Invalid JSON from %s" % path, status, path) from exc

            if status == 401 and not refreshed:
                self._logger.info("action=token_rejected path=%s", path)
                self.access_token(force_refresh=True)
                refreshed = True
                continue
            if status == 401:
                raise ArubaCentralAuthError("HTTP 401 from %s after token refresh: %s"
                                            % (path, _error_detail(response)))

            if (status == 429 or status >= 500) and attempt < self._max_retries:
                wait = _retry_after_seconds(response.headers.get("Retry-After"), self._clock())
                wait = min(MAX_RETRY_AFTER_S, wait) if wait is not None else self._backoff(attempt)
                self._logger.warning("action=retry reason=http_%d path=%s wait_s=%.1f", status, path, wait)
                self._sleep(wait)
                attempt += 1
                continue

            raise ArubaCentralApiError(
                "HTTP %d from %s: %s" % (status, path, _error_detail(response)), status, path
            )

    def paginate(self, path: str, params: Optional[Dict[str, Any]] = None,
                 limit: int = 100) -> Iterator[Dict[str, Any]]:
        """Yield all items of a cursor-paginated list endpoint ({items, count, total, next})."""
        base_params = dict(params or {})
        base_params["limit"] = limit
        cursor = None
        seen_cursors = set()
        for _page in range(MAX_PAGES):
            page_params = dict(base_params)
            if cursor is not None:
                page_params["next"] = cursor
            body = self.get_json(path, page_params)
            if not isinstance(body, dict):
                raise ArubaCentralApiError("Unexpected response type from %s" % path, 200, path)
            items = body.get("items") or []
            for item in items:
                yield item
            cursor = body.get("next")
            if cursor in (None, "") or not items:
                return
            if cursor in seen_cursors:
                self._logger.warning("action=pagination_stopped reason=repeated_cursor path=%s", path)
                return
            seen_cursors.add(cursor)
        self._logger.warning("action=pagination_stopped reason=max_pages path=%s", path)
