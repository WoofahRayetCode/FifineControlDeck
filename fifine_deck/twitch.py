"""Twitch Helix helpers for monitor keys (viewer count, stream uptime).

Uses the public Helix Get Streams endpoint with an App Access Token from the
Client Credentials grant. Credentials come from Options → Twitch settings
(Client-ID + Client Secret). Stdlib only — urllib + json — with short timeouts
so a down Twitch API cannot stall the monitor thread.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger(__name__)

HELIX_STREAMS = "https://api.twitch.tv/helix/streams"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HTTP_TIMEOUT = 3.0
# Cache a live/offline stream lookup briefly so viewers + uptime keys for the
# same channel share one Helix round-trip per tick window.
STREAM_CACHE_TTL = 8.0

_creds_provider: Optional[Callable[[], tuple[str, str]]] = None
_lock = threading.Lock()
_token: str = ""
_token_exp: float = 0.0
_token_client_id: str = ""
_stream_cache: dict[str, tuple[float, Optional["StreamInfo"]]] = {}


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


def invalidate() -> None:
    """Drop cached token and stream lookups (call after Options save)."""
    global _token, _token_exp, _token_client_id
    with _lock:
        _token = ""
        _token_exp = 0.0
        _token_client_id = ""
        _stream_cache.clear()


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
