"""Twitch Helix helpers for monitor keys (viewer count, stream uptime, ads).

Public endpoints (Get Streams) use an App Access Token from the Client
Credentials grant. Ads schedule / snooze need a broadcaster **user** OAuth
token (Options → Twitch → Log in) with channel:read:ads / channel:manage:ads.

Credentials come from Options → Twitch settings (Client-ID + Client Secret,
plus optional user tokens in the keyring). Stdlib only — urllib + json —
with short timeouts so a down Twitch API cannot stall the monitor thread.
"""
from __future__ import annotations

import json
import logging
import secrets
import select
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional

log = logging.getLogger(__name__)

HELIX_STREAMS = "https://api.twitch.tv/helix/streams"
HELIX_USERS = "https://api.twitch.tv/helix/users"
HELIX_ADS = "https://api.twitch.tv/helix/channels/ads"
HELIX_ADS_SNOOZE = "https://api.twitch.tv/helix/channels/ads/schedule/snooze"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
AUTH_URL = "https://id.twitch.tv/oauth2/authorize"
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
HTTP_TIMEOUT = 3.0
# Legacy loopback redirect (authorization-code flow). Prefer device-code
# login — it needs no local HTTP server. Keep these for docs / fallback.
OAUTH_REDIRECT_PORT = 18765
OAUTH_REDIRECT_URI = f"http://127.0.0.1:{OAUTH_REDIRECT_PORT}/callback"
OAUTH_REDIRECT_URI_LOCALHOST = f"http://localhost:{OAUTH_REDIRECT_PORT}/callback"
OAUTH_REDIRECT_URIS = (OAUTH_REDIRECT_URI, OAUTH_REDIRECT_URI_LOCALHOST)
OAUTH_SCOPES = "channel:read:ads channel:manage:ads"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# Cache a live/offline stream lookup briefly so viewers + uptime keys for the
# same channel share one Helix round-trip per tick window.
STREAM_CACHE_TTL = 8.0
AD_CACHE_TTL = 15.0

_creds_provider: Optional[Callable[[], tuple[str, str]]] = None
# Returns (access_token, refresh_token, user_id, user_login) from config/keyring.
_user_session_provider: Optional[Callable[[], tuple[str, str, str, str]]] = None
# Persist refreshed tokens: (access, refresh, user_id, user_login) -> None
_user_session_saver: Optional[Callable[[str, str, str, str], None]] = None
_lock = threading.Lock()
_token: str = ""
_token_exp: float = 0.0
_token_client_id: str = ""
_stream_cache: dict[str, tuple[float, Optional["StreamInfo"]]] = {}
_user_access: str = ""
_user_refresh: str = ""
_user_id: str = ""
_user_login: str = ""
_user_access_exp: float = 0.0
_ad_cache: dict[str, tuple[float, Optional["AdSchedule"]]] = {}


@dataclass(frozen=True)
class StreamInfo:
    """One Helix streams entry (or offline sentinel with live=False)."""
    live: bool
    login: str
    viewer_count: int = 0
    started_at: str = ""       # ISO-8601 from Helix, "" when offline
    title: str = ""
    game_name: str = ""

    @property
    def uptime_seconds(self) -> float | None:
        if not self.live or not self.started_at:
            return None
        try:
            # Helix uses Zulu timestamps: 2024-01-01T12:00:00Z
            started = datetime.fromisoformat(
                self.started_at.replace("Z", "+00:00"))
            return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        except (TypeError, ValueError):
            return None


def set_creds_provider(fn: Callable[[], tuple[str, str]] | None) -> None:
    """Inject (client_id, client_secret) lookup from the running config."""
    global _creds_provider
    _creds_provider = fn


def set_user_session_provider(
        fn: Callable[[], tuple[str, str, str, str]] | None) -> None:
    """Inject (access, refresh, user_id, user_login) from config/keyring."""
    global _user_session_provider
    _user_session_provider = fn


def set_user_session_saver(
        fn: Callable[[str, str, str, str], None] | None) -> None:
    """Inject saver called after a successful token refresh."""
    global _user_session_saver
    _user_session_saver = fn


def invalidate() -> None:
    """Drop cached tokens and Helix lookups (call after Options save)."""
    global _token, _token_exp, _token_client_id
    global _user_access, _user_refresh, _user_id, _user_login, _user_access_exp
    with _lock:
        _token = ""
        _token_exp = 0.0
        _token_client_id = ""
        _stream_cache.clear()
        _user_access = ""
        _user_refresh = ""
        _user_id = ""
        _user_login = ""
        _user_access_exp = 0.0
        _ad_cache.clear()


def invalidate_ad_cache() -> None:
    """Drop cached ad schedules (call after a successful snooze)."""
    with _lock:
        _ad_cache.clear()


def _http_json(method: str, url: str, headers: dict | None = None,
               body: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None and "Content-Type" not in (headers or {}):
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    if not raw:
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _app_access_token(client_id: str, client_secret: str) -> str:
    """Return a cached App Access Token, refreshing when near expiry."""
    global _token, _token_exp, _token_client_id
    now = time.monotonic()
    with _lock:
        if (_token and _token_client_id == client_id
                and now < _token_exp - 60):
            return _token
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }).encode()
    data = _http_json("POST", TOKEN_URL, body=body)
    token = str(data.get("access_token") or "")
    try:
        expires = float(data.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires = 0.0
    if not token:
        raise RuntimeError("Twitch token response missing access_token")
    with _lock:
        _token = token
        _token_client_id = client_id
        _token_exp = time.monotonic() + max(60.0, expires)
    return token


def _fetch_stream(login: str, client_id: str, client_secret: str) -> StreamInfo:
    token = _app_access_token(client_id, client_secret)
    qs = urllib.parse.urlencode({"user_login": login})
    url = f"{HELIX_STREAMS}?{qs}"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
    }
    try:
        data = _http_json("GET", url, headers=headers)
    except urllib.error.HTTPError as e:
        # 401: drop the cached token so the next sample re-auths.
        if e.code == 401:
            invalidate()
        raise
    rows = data.get("data") if isinstance(data.get("data"), list) else []
    if not rows:
        return StreamInfo(live=False, login=login)
    row = rows[0] if isinstance(rows[0], dict) else {}
    try:
        viewers = int(row.get("viewer_count") or 0)
    except (TypeError, ValueError):
        viewers = 0
    return StreamInfo(
        live=True,
        login=str(row.get("user_login") or login),
        viewer_count=max(0, viewers),
        started_at=str(row.get("started_at") or ""),
        title=str(row.get("title") or ""),
        game_name=str(row.get("game_name") or ""),
    )


def get_stream(login: str) -> StreamInfo | None:
    """Look up a channel's current stream. None = credentials / request error.

    Offline channels return StreamInfo(live=False), not None — so callers can
    tell "not live" from "could not ask Twitch".
    """
    login = (login or "").strip().lstrip("#").lower()
    if not login:
        return StreamInfo(live=False, login="")
    if _creds_provider is None:
        return None
    try:
        client_id, client_secret = _creds_provider()
    except Exception:
        log.debug("twitch creds provider failed", exc_info=True)
        return None
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id or not client_secret:
        return None

    now = time.monotonic()
    with _lock:
        cached = _stream_cache.get(login)
        if cached and now - cached[0] < STREAM_CACHE_TTL:
            return cached[1]

    try:
        info = _fetch_stream(login, client_id, client_secret)
    except Exception as e:
        log.warning("Twitch Helix lookup for %s failed: %s", login, e)
        with _lock:
            # Negative-cache briefly so a down API does not hammer every tick.
            _stream_cache[login] = (now, None)
        return None

    with _lock:
        _stream_cache[login] = (now, info)
    return info


def ping(client_id: str, client_secret: str,
         login: str = "") -> tuple[bool, str]:
    """Options → Test: validate Client-ID/secret (and optional channel)."""
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id or not client_secret:
        return False, "Client-ID and Client Secret are both required."
    try:
        _app_access_token(client_id, client_secret)
    except Exception as e:
        return False, f"Could not obtain an App Access Token:\n{e}"
    login = (login or "").strip().lstrip("#").lower()
    if not login:
        return True, "App Access Token OK. Add a channel login on a monitor key as Target."
    try:
        info = _fetch_stream(login, client_id, client_secret)
    except Exception as e:
        return False, f"Token OK, but Get Streams failed:\n{e}"
    if info.live:
        return True, (
            f"Live: {info.login} — {info.viewer_count} viewers, "
            f"{info.game_name or 'no category'}.")
    return True, f"Token OK. Channel '{login}' is offline right now."


def fmt_viewers(n: int) -> str:
    n = max(0, int(n))
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        text = f"{n / 1000:.1f}k"
        return text.replace(".0k", "k")
    text = f"{n / 1_000_000:.1f}M"
    return text.replace(".0M", "M")


def fmt_uptime(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {sec:02d}s"


# ---------------------------------------------------------------------------
# Broadcaster user OAuth (ads)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdSchedule:
    """One Get Ad Schedule / snooze response row."""
    next_ad_at: float | None = None   # unix epoch seconds, or None
    last_ad_at: float | None = None
    duration: int = 0                 # upcoming break length in seconds
    preroll_free_time: int = 0
    snooze_count: int = 0
    snooze_refresh_at: float | None = None

    @property
    def seconds_until_ad(self) -> float | None:
        if self.next_ad_at is None:
            return None
        return self.next_ad_at - time.time()


def fmt_ad_countdown(seconds: float) -> str:
    """Compact countdown for a monitor key face."""
    s = max(0, int(seconds))
    if s <= 0:
        return "NOW"
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m >= 10:
        return f"{m}m"
    return f"{m}:{sec:02d}"


def _parse_twitch_time(value) -> float | None:
    """Accept Unix epoch (int/float/str) or RFC3339 → unix seconds."""
    if value is None or value == "" or value == 0 or value == "0":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        # Real ad timestamps are epoch seconds (~1e9+); reject bare 0.
        return v if v > 1_000_000_000 else None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() or (text.replace(".", "", 1).isdigit() and "T" not in text):
        try:
            v = float(text)
            return v if v > 1_000_000_000 else None
        except (TypeError, ValueError):
            pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _ad_schedule_from_row(row: dict) -> AdSchedule:
    def _int(key: str) -> int:
        try:
            return max(0, int(float(row.get(key) or 0)))
        except (TypeError, ValueError):
            return 0
    return AdSchedule(
        next_ad_at=_parse_twitch_time(row.get("next_ad_at")),
        last_ad_at=_parse_twitch_time(row.get("last_ad_at")),
        duration=_int("duration"),
        preroll_free_time=_int("preroll_free_time"),
        snooze_count=_int("snooze_count"),
        snooze_refresh_at=_parse_twitch_time(row.get("snooze_refresh_at")),
    )


def user_login_status() -> tuple[bool, str]:
    """(logged_in, login_or_reason) for the Twitch settings dialog."""
    access, _refresh, user_id, login = _load_user_session()
    if access and user_id:
        return True, login or user_id
    return False, ""


def _load_user_session() -> tuple[str, str, str, str]:
    global _user_access, _user_refresh, _user_id, _user_login
    with _lock:
        if _user_access and _user_id:
            return _user_access, _user_refresh, _user_id, _user_login
    if _user_session_provider is None:
        return "", "", "", ""
    try:
        access, refresh, user_id, login = _user_session_provider()
    except Exception:
        log.debug("twitch user session provider failed", exc_info=True)
        return "", "", "", ""
    access = (access or "").strip()
    refresh = (refresh or "").strip()
    user_id = (user_id or "").strip()
    login = (login or "").strip().lstrip("#").lower()
    with _lock:
        _user_access = access
        _user_refresh = refresh
        _user_id = user_id
        _user_login = login
    return access, refresh, user_id, login


def _save_user_session(access: str, refresh: str, user_id: str,
                       login: str) -> None:
    global _user_access, _user_refresh, _user_id, _user_login, _user_access_exp
    with _lock:
        _user_access = access
        _user_refresh = refresh
        _user_id = user_id
        _user_login = login
        # Twitch user tokens are typically ~4h; refresh before we know expiry.
        _user_access_exp = time.monotonic() + 3 * 3600
    if _user_session_saver is not None:
        try:
            _user_session_saver(access, refresh, user_id, login)
        except Exception:
            log.warning("could not persist Twitch user tokens", exc_info=True)


def clear_user_session_cache() -> None:
    """Forget in-memory user tokens (after logout / Options clear)."""
    global _user_access, _user_refresh, _user_id, _user_login, _user_access_exp
    with _lock:
        _user_access = ""
        _user_refresh = ""
        _user_id = ""
        _user_login = ""
        _user_access_exp = 0.0
        _ad_cache.clear()


def _refresh_user_token(client_id: str, client_secret: str,
                        refresh: str) -> tuple[str, str]:
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }).encode()
    data = _http_json("POST", TOKEN_URL, body=body)
    access = str(data.get("access_token") or "")
    new_refresh = str(data.get("refresh_token") or refresh)
    if not access:
        raise RuntimeError("Twitch refresh response missing access_token")
    return access, new_refresh


def get_user_access_token() -> tuple[str, str, str] | None:
    """Return (access_token, user_id, user_login) or None if not logged in."""
    if _creds_provider is None:
        return None
    try:
        client_id, client_secret = _creds_provider()
    except Exception:
        return None
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id or not client_secret:
        return None
    access, refresh, user_id, login = _load_user_session()
    if not access or not user_id:
        return None
    now = time.monotonic()
    with _lock:
        exp = _user_access_exp
    if exp and now > exp - 120 and refresh:
        try:
            access, refresh = _refresh_user_token(
                client_id, client_secret, refresh)
            _save_user_session(access, refresh, user_id, login)
        except Exception as e:
            log.warning("Twitch user token refresh failed: %s", e)
            return None
    return access, user_id, login


def _helix_user_get(path_qs: str, access: str, client_id: str) -> dict:
    url = path_qs if path_qs.startswith("http") else f"https://api.twitch.tv{path_qs}"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access}",
    }
    try:
        return _http_json("GET", url, headers=headers)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            invalidate_ad_cache()
        raise


def open_url_nonblocking(url: str) -> None:
    """Open ``url`` without blocking the caller (GUI-safe).

    ``webbrowser.open`` can hang on Linux when a browser is already running or
    waiting on a lock; prefer ``xdg-open`` via ``Popen``, and only fall back to
    ``webbrowser`` on a daemon thread.
    """
    import shutil
    import subprocess

    xdg = shutil.which("xdg-open")
    if xdg:
        try:
            subprocess.Popen(  # noqa: S603
                [xdg, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except OSError as e:
            log.debug("xdg-open failed: %s", e)
    threading.Thread(
        target=lambda: webbrowser.open(url),
        name="twitch-open-browser",
        daemon=True,
    ).start()


class DeviceLoginSession:
    """Cancellable Twitch device-code OAuth (no localhost callback).

    1. :meth:`begin` → user_code + verification_uri (open in browser)
    2. :meth:`finish` on a worker thread → poll until approved / cancel / expiry
    """

    def __init__(self, client_id: str, client_secret: str,
                 timeout: float = 600.0):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.timeout = max(60.0, float(timeout))
        self.user_code = ""
        self.verification_uri = ""
        self.verification_uri_complete = ""
        self._device_code = ""
        self._interval = 5.0
        self._cancel = threading.Event()

    def begin(self) -> str:
        """Request a device code; return the URL the user should open."""
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Client-ID and Client Secret are both required.")
        self._cancel.clear()
        body = urllib.parse.urlencode({
            "client_id": self.client_id,
            "scopes": OAUTH_SCOPES,
        }).encode()
        try:
            data = _http_json("POST", DEVICE_URL, body=body)
        except Exception as e:
            raise RuntimeError(f"Twitch device login failed to start:\n{e}") from e
        self._device_code = str(data.get("device_code") or "")
        self.user_code = str(data.get("user_code") or "")
        self.verification_uri = str(
            data.get("verification_uri")
            or "https://www.twitch.tv/activate")
        complete = data.get("verification_uri_complete")
        self.verification_uri_complete = (
            str(complete) if complete else self.verification_uri)
        try:
            self._interval = max(3.0, float(data.get("interval") or 5))
        except (TypeError, ValueError):
            self._interval = 5.0
        try:
            expires = float(data.get("expires_in") or 1800)
            self.timeout = min(self.timeout, max(60.0, expires))
        except (TypeError, ValueError):
            pass
        if not self._device_code or not self.user_code:
            raise RuntimeError("Twitch device response missing codes.")
        return self.verification_uri_complete or self.verification_uri

    def cancel(self) -> None:
        self._cancel.set()

    def finish(self) -> tuple[bool, str, str, str, str, str]:
        """Poll Twitch until the user approves (or cancel / timeout)."""
        if not self._device_code:
            return False, "Login was not started.", "", "", "", ""
        deadline = time.monotonic() + self.timeout
        interval = self._interval
        first = True
        while not self._cancel.is_set() and time.monotonic() < deadline:
            if not first:
                # Sleep in small slices so Cancel stays responsive.
                end = time.monotonic() + interval
                while time.monotonic() < end and not self._cancel.is_set():
                    time.sleep(0.1)
            first = False
            if self._cancel.is_set():
                break
            body = urllib.parse.urlencode({
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "device_code": self._device_code,
                "grant_type": DEVICE_GRANT,
            }).encode()
            try:
                req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
                req.add_header("Content-Type",
                               "application/x-www-form-urlencoded")
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                if not isinstance(data, dict):
                    data = {}
            except urllib.error.HTTPError as e:
                try:
                    err = json.loads(e.read().decode("utf-8", errors="replace"))
                except Exception:
                    err = {}
                if not isinstance(err, dict):
                    err = {}
                status = str(err.get("message") or err.get("status")
                             or err.get("error") or "")
                # Twitch uses error field: authorization_pending | slow_down
                err_code = str(err.get("error") or "")
                if e.code == 400 and err_code in (
                        "authorization_pending", "slow_down"):
                    if err_code == "slow_down":
                        interval = min(30.0, interval + 5.0)
                    continue
                if e.code == 400 and err_code in (
                        "expired_token", "access_denied"):
                    return False, f"Twitch login {err_code}.", "", "", "", ""
                return False, (
                    f"Twitch token poll failed ({e.code}): "
                    f"{status or e.reason}"), "", "", "", ""
            except Exception as e:
                log.debug("device token poll error: %s", e)
                continue

            access = str(data.get("access_token") or "")
            refresh = str(data.get("refresh_token") or "")
            if not access:
                continue
            if not refresh:
                return False, "Token response missing refresh token.", \
                    "", "", "", ""
            try:
                users = _helix_user_get(HELIX_USERS, access, self.client_id)
            except Exception as e:
                return False, f"Logged in, but Get Users failed:\n{e}", \
                    "", "", "", ""
            rows = users.get("data") if isinstance(users.get("data"), list) else []
            if not rows or not isinstance(rows[0], dict):
                return False, "Get Users returned no profile.", "", "", "", ""
            user_id = str(rows[0].get("id") or "")
            login = str(rows[0].get("login") or "").lower()
            if not user_id:
                return False, "Get Users missing id.", "", "", "", ""
            _save_user_session(access, refresh, user_id, login)
            return (True, f"Logged in as {login or user_id}.",
                    access, refresh, user_id, login)

        if self._cancel.is_set():
            return False, "Login cancelled.", "", "", "", ""
        return False, "Login timed out — try again.", "", "", "", ""


class UserLoginSession:
    """Legacy loopback OAuth (authorization code). Prefer DeviceLoginSession.

    Bind + accept run on the **same worker thread** (call :meth:`begin` then
    :meth:`finish` there). The GUI opens :attr:`auth_url` with
    :func:`open_url_nonblocking` once :meth:`begin` returns.

    Listens on both ``127.0.0.1`` and ``::1`` so a browser that resolves
    ``localhost`` to IPv6 still reaches the callback.
    """

    def __init__(self, client_id: str, client_secret: str,
                 timeout: float = 180.0):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.timeout = max(30.0, float(timeout))
        self.auth_url = ""
        self._state = ""
        self._servers: list[HTTPServer] = []
        self._result: dict = {"code": "", "error": "", "done": False}
        self._cancel = threading.Event()

    def begin(self) -> str:
        """Bind loopback redirect servers and return the Twitch authorize URL."""
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Client-ID and Client Secret are both required.")
        self._state = secrets.token_urlsafe(16)
        self._result = {"code": "", "error": "", "done": False}
        self._cancel.clear()
        self._close_servers()
        session = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path not in ("/callback", "/"):
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                if qs.get("state", [""])[0] != session._state:
                    session._result["error"] = "OAuth state mismatch"
                elif "error" in qs:
                    session._result["error"] = qs.get(
                        "error_description", qs["error"])[0]
                else:
                    session._result["code"] = qs.get("code", [""])[0]
                session._result["done"] = True
                body = (
                    "<html><body style='font-family:sans-serif;padding:2rem'>"
                    "<h2>Fifine Control Deck</h2>"
                    "<p>Twitch login complete — you can close this tab.</p>"
                    "</body></html>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):  # noqa: A003
                return

        errors: list[str] = []
        # IPv4 loopback
        try:
            srv = HTTPServer(("127.0.0.1", OAUTH_REDIRECT_PORT), Handler)
            srv.timeout = 0.5
            self._servers.append(srv)
        except OSError as e:
            errors.append(f"127.0.0.1: {e}")

        # IPv6 loopback — Chrome often uses ::1 for "localhost"
        try:
            class HTTPServerV6(HTTPServer):
                address_family = socket.AF_INET6

            srv6 = HTTPServerV6(("::1", OAUTH_REDIRECT_PORT), Handler)
            srv6.timeout = 0.5
            self._servers.append(srv6)
        except OSError as e:
            errors.append(f"::1: {e}")

        if not self._servers:
            raise RuntimeError(
                f"Could not bind port {OAUTH_REDIRECT_PORT} "
                f"({'; '.join(errors)}). "
                f"Is another Fifine login still open?"
            )

        qs = urllib.parse.urlencode({
            "client_id": self.client_id,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": OAUTH_SCOPES,
            "state": self._state,
            "force_verify": "true",
        })
        self.auth_url = f"{AUTH_URL}?{qs}"
        return self.auth_url

    def cancel(self) -> None:
        """Abort :meth:`finish` (safe from the GUI thread)."""
        self._cancel.set()

    def _close_servers(self) -> None:
        for srv in self._servers:
            try:
                srv.server_close()
            except Exception:
                pass
        self._servers = []

    def finish(self) -> tuple[bool, str, str, str, str, str]:
        """Wait for the redirect, exchange the code, save the session.

        Blocks the calling thread until success, cancel, or timeout. Must not
        run on the Qt GUI thread.
        """
        if not self._servers:
            return False, "Login was not started.", "", "", "", ""
        deadline = time.monotonic() + self.timeout
        try:
            while (not self._result["done"]
                   and not self._cancel.is_set()
                   and time.monotonic() < deadline):
                socks = {srv.socket: srv for srv in self._servers}
                try:
                    ready, _, _ = select.select(
                        list(socks.keys()), [], [], 0.5)
                except (ValueError, OSError):
                    break
                if not ready:
                    continue
                for sock in ready:
                    srv = socks.get(sock)
                    if srv is not None:
                        srv.handle_request()
        finally:
            self._close_servers()

        if self._cancel.is_set():
            return False, "Login cancelled.", "", "", "", ""
        if not self._result["done"]:
            return False, "Login timed out — try again.", "", "", "", ""
        if self._result["error"]:
            return False, str(self._result["error"]), "", "", "", ""
        code = self._result["code"]
        if not code:
            return False, "Twitch did not return an authorization code.", \
                "", "", "", ""

        body = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": OAUTH_REDIRECT_URI,
        }).encode()
        try:
            data = _http_json("POST", TOKEN_URL, body=body)
        except Exception as e:
            return False, f"Token exchange failed:\n{e}", "", "", "", ""
        access = str(data.get("access_token") or "")
        refresh = str(data.get("refresh_token") or "")
        if not access or not refresh:
            return False, "Token response missing access/refresh token.", \
                "", "", "", ""

        try:
            users = _helix_user_get(HELIX_USERS, access, self.client_id)
        except Exception as e:
            return False, f"Logged in, but Get Users failed:\n{e}", \
                "", "", "", ""
        rows = users.get("data") if isinstance(users.get("data"), list) else []
        if not rows or not isinstance(rows[0], dict):
            return False, "Get Users returned no profile.", "", "", "", ""
        user_id = str(rows[0].get("id") or "")
        login = str(rows[0].get("login") or "").lower()
        if not user_id:
            return False, "Get Users missing id.", "", "", "", ""
        _save_user_session(access, refresh, user_id, login)
        return (True, f"Logged in as {login or user_id}.",
                access, refresh, user_id, login)


def start_user_login(client_id: str, client_secret: str,
                     timeout: float = 600.0,
                     *, cancel_event: threading.Event | None = None,
                     open_browser: bool = True
                     ) -> tuple[bool, str, str, str, str, str]:
    """Device-code OAuth; block until the user approves (or timeout).

    Prefer :class:`DeviceLoginSession` from the GUI so the wait can run off
    the UI thread. No localhost callback is required.

    Returns (ok, message, access, refresh, user_id, user_login).
    """
    session = DeviceLoginSession(client_id, client_secret, timeout=timeout)
    try:
        url = session.begin()
    except RuntimeError as e:
        return False, str(e), "", "", "", ""
    if cancel_event is not None:
        def _watch():
            cancel_event.wait()
            session.cancel()
        threading.Thread(target=_watch, daemon=True).start()
    if open_browser:
        open_url_nonblocking(url)
    return session.finish()


def get_ad_schedule(broadcaster_id: str = "") -> AdSchedule | None:
    """Fetch the broadcaster's ad schedule. None = not logged in / error."""
    tok = get_user_access_token()
    if tok is None:
        return None
    access, user_id, login = tok
    if _creds_provider is None:
        return None
    try:
        client_id, _secret = _creds_provider()
    except Exception:
        return None
    client_id = (client_id or "").strip()
    if not client_id:
        return None
    bid = (broadcaster_id or user_id or "").strip()
    if not bid:
        return None

    now = time.monotonic()
    with _lock:
        cached = _ad_cache.get(bid)
        if cached and now - cached[0] < AD_CACHE_TTL:
            return cached[1]

    qs = urllib.parse.urlencode({"broadcaster_id": bid})
    try:
        data = _helix_user_get(f"{HELIX_ADS}?{qs}", access, client_id)
    except Exception as e:
        log.warning("Twitch Get Ad Schedule failed: %s", e)
        # On 401 try one refresh then retry.
        if isinstance(e, urllib.error.HTTPError) and e.code == 401:
            try:
                _secret = (_creds_provider() or ("", ""))[1]
                access2, refresh2, uid2, login2 = _load_user_session()
                if refresh2:
                    access2, refresh2 = _refresh_user_token(
                        client_id, _secret, refresh2)
                    _save_user_session(access2, refresh2, uid2, login2)
                    data = _helix_user_get(
                        f"{HELIX_ADS}?{qs}", access2, client_id)
                else:
                    with _lock:
                        _ad_cache[bid] = (now, None)
                    return None
            except Exception as e2:
                log.warning("Twitch Get Ad Schedule retry failed: %s", e2)
                with _lock:
                    _ad_cache[bid] = (now, None)
                return None
        else:
            with _lock:
                _ad_cache[bid] = (now, None)
            return None

    rows = data.get("data") if isinstance(data.get("data"), list) else []
    if not rows or not isinstance(rows[0], dict):
        sched = AdSchedule()
    else:
        sched = _ad_schedule_from_row(rows[0])
    with _lock:
        _ad_cache[bid] = (now, sched)
    return sched


def snooze_next_ad() -> tuple[bool, str]:
    """Push the next automatic mid-roll back by ~5 minutes."""
    tok = get_user_access_token()
    if tok is None:
        return False, "Log in with Twitch under Options → Twitch settings first."
    access, user_id, login = tok
    if _creds_provider is None:
        return False, "Twitch Client-ID / Secret not configured."
    try:
        client_id, client_secret = _creds_provider()
    except Exception:
        return False, "Twitch credentials unavailable."
    client_id = (client_id or "").strip()
    if not client_id:
        return False, "Twitch Client-ID missing."
    qs = urllib.parse.urlencode({"broadcaster_id": user_id})
    url = f"{HELIX_ADS_SNOOZE}?{qs}"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access}",
    }
    try:
        data = _http_json("POST", url, headers=headers, body=b"")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        if e.code == 429:
            return False, "No snoozes left right now."
        if e.code == 400:
            return False, (
                "Cannot snooze (not live, or no upcoming scheduled ad)."
                + (f"\n{err_body}" if err_body else ""))
        if e.code == 401 and client_secret:
            try:
                _a, refresh, uid, ulogin = _load_user_session()
                if refresh:
                    access, refresh = _refresh_user_token(
                        client_id, client_secret, refresh)
                    _save_user_session(access, refresh, uid, ulogin)
                    headers["Authorization"] = f"Bearer {access}"
                    data = _http_json("POST", url, headers=headers, body=b"")
                else:
                    return False, f"Twitch auth failed ({e.code})."
            except Exception as e2:
                return False, f"Twitch snooze failed: {e2}"
        else:
            return False, f"Twitch snooze failed ({e.code})."
    except Exception as e:
        return False, f"Twitch snooze failed: {e}"

    invalidate_ad_cache()
    rows = data.get("data") if isinstance(data.get("data"), list) else []
    if rows and isinstance(rows[0], dict):
        sched = _ad_schedule_from_row(rows[0])
        with _lock:
            _ad_cache[user_id] = (time.monotonic(), sched)
        left = sched.snooze_count
        when = ""
        if sched.seconds_until_ad is not None and sched.seconds_until_ad > 0:
            when = f" Next ad in {fmt_ad_countdown(sched.seconds_until_ad)}."
        return True, f"Ad snoozed ({left} left).{when}"
    return True, "Ad snoozed."
