"""OBS WebSocket v5 client: auth math, framing, reconnect helpers."""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import threading
from unittest.mock import MagicMock

import pytest

from fifine_deck import obs_ws


def test_auth_string_matches_protocol_algorithm():
    # Protocol: secret = b64(sha256(password + salt));
    # auth = b64(sha256(secret + challenge)). The illustrative "expected"
    # string in protocol.md does not match this algorithm, so we assert the
    # steps themselves (same construction as simpleobsws / obsws-python).
    password = "supersecretpassword"
    salt = "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI="
    challenge = "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY="
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest())
    expected = base64.b64encode(
        hashlib.sha256(secret + challenge.encode("utf-8")).digest()
    ).decode("ascii")
    assert obs_ws.auth_string(password, salt, challenge) == expected
    assert obs_ws.auth_string(password, salt, challenge) == \
        "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4="


def test_auth_string_empty_password_still_hashes():
    # OBS can require auth with an empty password; we must still produce a
    # challenge response rather than omitting the field.
    out = obs_ws.auth_string("", "salt", "challenge")
    assert isinstance(out, str) and len(out) > 10


def _server_frame(text: str) -> bytes:
    """Build an unmasked server→client text frame."""
    payload = text.encode("utf-8")
    plen = len(payload)
    if plen < 126:
        return bytes([0x81, plen]) + payload
    return bytes([0x81, 126]) + struct.pack("!H", plen) + payload


def _client_frame_payload(data: bytes) -> bytes:
    """Extract payload from a masked client→server text/control frame."""
    assert data[0] & 0x0F in (0x1, 0x8, 0x9, 0xA)
    b1 = data[1]
    assert b1 & 0x80  # masked
    plen = b1 & 0x7F
    i = 2
    if plen == 126:
        plen = struct.unpack("!H", data[2:4])[0]
        i = 4
    mask = data[i:i + 4]
    payload = data[i + 4:i + 4 + plen]
    return bytes(b ^ mask[j % 4] for j, b in enumerate(payload))


class FakeSock:
    """Minimal socket stand-in for ObsClient unit tests."""

    def __init__(self, inbound: list[bytes]):
        self._inbound = list(inbound)
        self._buf = b""
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def recv(self, n):
        if not self._buf and self._inbound:
            self._buf = self._inbound.pop(0)
        if not self._buf:
            raise TimeoutError("recv timeout")
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def sendall(self, data):
        self.sent.append(bytes(data))

    def shutdown(self, *_):
        pass

    def close(self):
        self.closed = True


def test_client_identify_and_request(monkeypatch):
    hello = json.dumps({
        "op": 0,
        "d": {
            "obsStudioVersion": "32.0.0",
            "obsWebSocketVersion": "5.5.2",
            "rpcVersion": 1,
            "authentication": {
                "salt": "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=",
                "challenge": "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY=",
            },
        },
    })
    identified = json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}})
    response = json.dumps({
        "op": 7,
        "d": {
            "requestType": "GetVersion",
            "requestId": "WILL_REPLACE",
            "requestStatus": {"result": True, "code": 100},
            "responseData": {"obsVersion": "32.0.0",
                             "obsWebSocketVersion": "5.5.2"},
        },
    })

    # Handshake HTTP response, then WS frames for Hello / Identified / Response.
    key_accept_placeholder = "pending"
    http = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: PLACEHOLDER\r\n"
        b"\r\n"
    )

    state = {"sock": None, "accept": None}

    def fake_create_connection(addr, timeout=None):
        # Build accept from the key the client will send — we patch after
        # handshake starts by rewriting on first sendall. Simpler: compute
        # accept lazily when handshake reads.
        sock = FakeSock([])
        state["sock"] = sock

        original_sendall = sock.sendall

        def sendall(data):
            original_sendall(data)
            if data.startswith(b"GET ") and not sock._inbound:
                # Parse Sec-WebSocket-Key and build a valid 101 + frames.
                text = data.decode("ascii", "replace")
                key = None
                for line in text.split("\r\n"):
                    if line.lower().startswith("sec-websocket-key:"):
                        key = line.split(":", 1)[1].strip()
                assert key
                accept = obs_ws._ws_accept_key(key)
                # Response requestId is unknown until Identify+Request are sent;
                # we patch the GetVersion response after the request frame.
                frames = (
                    _server_frame(hello)
                    + _server_frame(identified)
                )
                http_ok = (
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\n"
                    b"Connection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: " + accept.encode("ascii") + b"\r\n"
                    b"\r\n"
                )
                sock._inbound.append(http_ok + frames)

        sock.sendall = sendall
        return sock

    monkeypatch.setattr(obs_ws.socket, "create_connection",
                        fake_create_connection)

    client = obs_ws.ObsClient("127.0.0.1", 4455, "supersecretpassword")
    client.connect()
    assert client.obs_studio_version == "32.0.0"
    assert client.connected

    # After Identify, inject the response matching the next requestId.
    sock = state["sock"]
    assert sock is not None

    # Find the Identify payload and check auth string.
    identify_payloads = []
    for chunk in sock.sent:
        if chunk.startswith(b"GET "):
            continue
        try:
            payload = _client_frame_payload(chunk)
            msg = json.loads(payload.decode("utf-8"))
            if msg.get("op") == 1:
                identify_payloads.append(msg)
        except Exception:
            continue
    assert identify_payloads
    auth = identify_payloads[0]["d"]["authentication"]
    assert auth == obs_ws.auth_string(
        "supersecretpassword",
        "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=",
        "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY=")

    # Queue a response that mirrors whatever requestId the client picks.
    def call_with_dynamic_response():
        # Intercept sendall during call to learn requestId, then feed response.
        orig = sock.sendall

        def sendall(data):
            orig(data)
            if data.startswith(b"GET "):
                return
            try:
                payload = _client_frame_payload(data)
                msg = json.loads(payload.decode("utf-8"))
            except Exception:
                return
            if msg.get("op") == 6:
                rid = msg["d"]["requestId"]
                resp = {
                    "op": 7,
                    "d": {
                        "requestType": "GetVersion",
                        "requestId": rid,
                        "requestStatus": {"result": True, "code": 100},
                        "responseData": {
                            "obsVersion": "32.0.0",
                            "obsWebSocketVersion": "5.5.2",
                        },
                    },
                }
                sock._inbound.append(_server_frame(json.dumps(resp)))

        sock.sendall = sendall
        return client.call("GetVersion")

    data = call_with_dynamic_response()
    assert data["obsVersion"] == "32.0.0"
    client.close()


def test_call_helper_never_raises_when_obs_down(monkeypatch):
    obs_ws.invalidate()

    def boom(*a, **k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(obs_ws.socket, "create_connection", boom)
    assert obs_ws.call("127.0.0.1", 4455, "", "GetVersion") is None


def test_invalidate_drops_cache(monkeypatch):
    obs_ws.invalidate()
    fake = MagicMock()
    fake.connected = True
    with obs_ws._cache_lock:
        obs_ws._cached = fake
        obs_ws._cached_key = ("127.0.0.1", 4455, "")
    obs_ws.invalidate()
    fake.close.assert_called_once()
    assert obs_ws._cached is None


def test_parse_ws_url():
    assert obs_ws.parse_ws_url("ws://192.168.1.5:4456") == ("192.168.1.5", 4456)
    assert obs_ws.parse_ws_url("127.0.0.1")[0] == "127.0.0.1"
