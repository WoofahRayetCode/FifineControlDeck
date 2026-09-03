"""Minimal synchronous OBS WebSocket v5 client (stdlib only).

Talks to OBS Studio's built-in obs-websocket (OBS 28+, default port 4455)
using only the Python standard library: TCP sockets, a hand-rolled WebSocket
framing layer, JSON, and SHA-256 for the Identify authentication challenge.

Designed for keypress actions:
- Short connect/request timeouts so a down OBS cannot freeze the deck.
- Shared cached connection with reconnect-once on stale sockets.
- No event subscriptions (request/response only).
- Never raises into callers of the module-level ``call()`` / ``ping()``
  helpers used by the action engine — those log and return None.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import socket
import struct
import threading
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

RPC_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4455
CONNECT_TIMEOUT = 2.0
REQUEST_TIMEOUT = 5.0

# WebSocket close codes from obs-websocket that matter to us.
_AUTH_FAILED = 4009
_SESSION_INVALIDATED = 4011


class ObsError(Exception):
    """Raised by ObsClient for protocol / transport failures."""

    def __init__(self, message: str, *, close_code: int | None = None,
                 request_code: int | None = None):
        super().__init__(message)
        self.close_code = close_code
        self.request_code = request_code


def auth_string(password: str, salt: str, challenge: str) -> str:
    """Build the Identify authentication string (obs-websocket v5)."""
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest())
    return base64.b64encode(
        hashlib.sha256(secret + challenge.encode("utf-8")).digest()
    ).decode("ascii")


# ---------------------------------------------------------------------------
# Minimal WebSocket client (client → server frames only; text JSON)
# ---------------------------------------------------------------------------

def _ws_accept_key(key: str) -> str:
    guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    digest = hashlib.sha1((key + guid).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _ws_handshake(sock: socket.socket, host: str, port: int) -> bytes:
    """Perform the HTTP upgrade. Returns any bytes already read past headers."""
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    host_hdr = host if port == 80 else f"{host}:{port}"
    req = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {host_hdr}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Protocol: obswebsocket.json\r\n"
        f"\r\n"
    ).encode("ascii")
    sock.sendall(req)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ObsError("connection closed during WebSocket handshake")
        data += chunk
        if len(data) > 65536:
            raise ObsError("WebSocket handshake response too large")
    header, _sep, rest = data.partition(b"\r\n\r\n")
    status_line = header.split(b"\r\n", 1)[0].decode("ascii", "replace")
    if " 101 " not in status_line:
        raise ObsError(f"WebSocket upgrade failed: {status_line}")
    headers = {}
    for line in header.split(b"\r\n")[1:]:
        if b":" not in line:
            continue
        k, v = line.split(b":", 1)
        headers[k.strip().lower()] = v.strip()
    accept = headers.get(b"sec-websocket-accept", b"").decode("ascii")
    if accept != _ws_accept_key(key):
        raise ObsError("WebSocket Sec-WebSocket-Accept mismatch")
    return rest


def _ws_send_text(sock: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    plen = len(payload)
    header = bytearray([0x81])  # FIN + text
    if plen < 126:
        header.append(0x80 | plen)
    elif plen < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", plen))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", plen))
    mask = secrets.token_bytes(4)
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + masked)


def _ws_send_control(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    if len(payload) > 125:
        payload = payload[:125]
    header = bytearray([0x80 | (opcode & 0x0F), 0x80 | len(payload)])
    mask = secrets.token_bytes(4)
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + masked)


def _ws_close(sock: socket.socket) -> None:
    try:
        _ws_send_control(sock, 0x8, struct.pack("!H", 1000))
    except OSError:
        pass
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# OBS client
# ---------------------------------------------------------------------------

class ObsClient:
    """One identified session to an obs-websocket v5 server."""

    def __init__(self, host: str, port: int, password: str = "",
                 connect_timeout: float = CONNECT_TIMEOUT,
                 request_timeout: float = REQUEST_TIMEOUT):
        self.host = host
        self.port = int(port)
        self.password = password or ""
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self._sock: socket.socket | None = None
        self._leftover = b""
        self._lock = threading.Lock()
        self._kicked = False
        self.obs_studio_version = ""
        self.obs_ws_version = ""

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._kicked

    def connect(self) -> None:
        if self._kicked:
            raise ObsError("session was invalidated by OBS; not reconnecting",
                           close_code=_SESSION_INVALIDATED)
        sock = socket.create_connection(
            (self.host, self.port), timeout=self.connect_timeout)
        sock.settimeout(self.request_timeout)
        try:
            self._leftover = _ws_handshake(sock, self.host, self.port)
            self._sock = sock
            hello = self._read_op(expected=0)
            d = hello.get("d") or {}
            self.obs_studio_version = str(d.get("obsStudioVersion", ""))
            self.obs_ws_version = str(d.get("obsWebSocketVersion", ""))
            identify: dict[str, Any] = {
                "rpcVersion": RPC_VERSION,
                "eventSubscriptions": 0,  # request/response only
            }
            auth = d.get("authentication")
            if auth:
                identify["authentication"] = auth_string(
                    self.password,
                    str(auth.get("salt", "")),
                    str(auth.get("challenge", "")),
                )
            _ws_send_text(sock, json.dumps({"op": 1, "d": identify}))
            self._read_op(expected=2)
        except ObsError as e:
            self._sock = None
            _ws_close(sock)
            if e.close_code == _AUTH_FAILED:
                raise ObsError(
                    "OBS authentication failed — check Options → OBS settings "
                    "(password)",
                    close_code=_AUTH_FAILED) from e
            if e.close_code == _SESSION_INVALIDATED:
                self._kicked = True
            raise
        except OSError as e:
            self._sock = None
            _ws_close(sock)
            raise ObsError(
                f"could not connect to OBS at {self.host}:{self.port}: {e}"
            ) from e

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            self._leftover = b""
        if sock is not None:
            _ws_close(sock)

    def call(self, request_type: str,
             request_data: dict | None = None) -> dict:
        """Send a Request and return responseData (may be empty)."""
        with self._lock:
            if self._sock is None:
                raise ObsError("not connected")
            sock = self._sock
            req_id = uuid.uuid4().hex
            body: dict[str, Any] = {
                "requestType": request_type,
                "requestId": req_id,
            }
            if request_data is not None:
                body["requestData"] = request_data
            try:
                _ws_send_text(sock, json.dumps({"op": 6, "d": body}))
                while True:
                    msg = self._read_json()
                    op = msg.get("op")
                    if op == 5:
                        continue  # stray event; we asked for none
                    if op == 7:
                        d = msg.get("d") or {}
                        if d.get("requestId") != req_id:
                            continue
                        status = d.get("requestStatus") or {}
                        if not status.get("result"):
                            code = status.get("code")
                            comment = status.get("comment") or ""
                            raise ObsError(
                                f"OBS request {request_type} failed"
                                + (f" ({code}: {comment})" if code is not None
                                   else ""),
                                request_code=code if isinstance(code, int)
                                else None)
                        return d.get("responseData") or {}
                    if op == 2:
                        continue
                    raise ObsError(
                        f"unexpected OBS opcode while waiting for "
                        f"response: {op}")
            except ObsError as e:
                if e.close_code == _SESSION_INVALIDATED:
                    self._kicked = True
                try:
                    _ws_close(sock)
                finally:
                    self._sock = None
                    self._leftover = b""
                raise
            except OSError as e:
                try:
                    _ws_close(sock)
                finally:
                    self._sock = None
                    self._leftover = b""
                raise ObsError(f"OBS connection lost: {e}") from e

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            if self._leftover:
                take = min(n - len(buf), len(self._leftover))
                buf.extend(self._leftover[:take])
                self._leftover = self._leftover[take:]
                continue
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ObsError("connection closed while reading")
            buf.extend(chunk)
        return bytes(buf)

    def _recv_message(self) -> str:
        """Receive one complete text WebSocket message."""
        assert self._sock is not None
        parts: list[bytes] = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            plen = b1 & 0x7F
            if plen == 126:
                plen = struct.unpack("!H", self._recv_exact(2))[0]
            elif plen == 127:
                plen = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(plen)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                code = (struct.unpack("!H", payload[:2])[0]
                        if len(payload) >= 2 else None)
                reason = (payload[2:].decode("utf-8", "replace")
                          if len(payload) > 2 else "")
                raise ObsError(
                    "OBS closed the connection"
                    + (f" ({code}: {reason})" if code is not None else ""),
                    close_code=code)
            if opcode == 0x9:
                _ws_send_control(self._sock, 0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x0):
                parts.append(payload)
                if fin:
                    break
                continue
            if opcode == 0x2:
                raise ObsError("unexpected binary WebSocket frame from OBS")
            if fin and not parts:
                continue
        return b"".join(parts).decode("utf-8")

    def _read_json(self) -> dict:
        raw = self._recv_message()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ObsError(f"invalid JSON from OBS: {e}") from e
        if not isinstance(msg, dict):
            raise ObsError("OBS message was not a JSON object")
        return msg

    def _read_op(self, expected: int) -> dict:
        msg = self._read_json()
        if msg.get("op") != expected:
            raise ObsError(
                f"expected OBS opcode {expected}, got {msg.get('op')}")
        return msg


# ---------------------------------------------------------------------------
# Process-wide cached client
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cached: ObsClient | None = None
_cached_key: tuple[str, int, str] | None = None


def invalidate() -> None:
    """Drop the cached connection (call after settings change)."""
    global _cached, _cached_key
    with _cache_lock:
        client, _cached = _cached, None
        _cached_key = None
    if client is not None:
        client.close()


def _key(host: str, port: int, password: str) -> tuple[str, int, str]:
    return (host or DEFAULT_HOST, int(port or DEFAULT_PORT), password or "")


def _get_client(host: str, port: int, password: str) -> ObsClient:
    global _cached, _cached_key
    k = _key(host, port, password)
    with _cache_lock:
        if _cached is not None and _cached_key == k and _cached.connected:
            return _cached
        if _cached is not None:
            old, _cached = _cached, None
            _cached_key = None
        else:
            old = None
    if old is not None:
        old.close()
    client = ObsClient(k[0], k[1], k[2])
    client.connect()
    with _cache_lock:
        _cached = client
        _cached_key = k
    return client


def call(host: str, port: int, password: str, request_type: str,
         request_data: dict | None = None) -> Optional[dict]:
    """Run one OBS request. Returns responseData, or None on failure.

    Reconnects once if the cached socket is stale. Never raises.
    """
    host = (host or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        port_i = DEFAULT_PORT
    password = password or ""

    for attempt in (0, 1):
        try:
            client = _get_client(host, port_i, password)
            return client.call(request_type, request_data)
        except ObsError as e:
            if e.close_code == _SESSION_INVALIDATED:
                log.warning("OBS: %s", e)
                invalidate()
                return None
            if e.close_code == _AUTH_FAILED:
                log.warning("OBS: %s", e)
                invalidate()
                return None
            if attempt == 0:
                invalidate()
                continue
            log.warning("OBS %s failed: %s", request_type, e)
            return None
        except Exception as e:  # noqa: BLE001 — action path must not raise
            log.warning("OBS %s failed: %s", request_type, e)
            invalidate()
            return None
    return None


def ping(host: str, port: int, password: str) -> tuple[bool, str]:
    """Test connectivity. Returns (ok, message) for the settings dialog."""
    invalidate()
    host = (host or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return False, f"invalid port: {port!r}"
    try:
        client = ObsClient(host, port_i, password or "")
        client.connect()
        data = client.call("GetVersion")
        client.close()
        studio = data.get("obsVersion") or client.obs_studio_version or "?"
        ws = data.get("obsWebSocketVersion") or client.obs_ws_version or "?"
        return True, f"Connected to OBS {studio} (websocket {ws}) at {host}:{port_i}"
    except ObsError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def parse_ws_url(url: str) -> tuple[str, int]:
    """Optional helper: parse ws://host:port into (host, port)."""
    u = urlparse(url if "://" in url else f"ws://{url}")
    host = u.hostname or DEFAULT_HOST
    port = u.port or DEFAULT_PORT
    return host, port
