"""Anonymous Twitch IRC chat reader for page display_mode=twitch_chat.

Connects as a justinfanNNNN viewer (read-only, no OAuth) and emits PRIVMSG
lines. Stdlib sockets only — keeps the app free of extra chat dependencies.
"""
from __future__ import annotations

import logging
import random
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional

log = logging.getLogger(__name__)

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6667
RECV_TIMEOUT = 1.0
MAX_MESSAGES = 200
RECONNECT_MIN = 2.0
RECONNECT_MAX = 30.0

_PRIVMSG_RE = re.compile(
    r"^(?:@(?P<tags>[^ ]+) )?(?::(?P<prefix>[^ ]+) )?(?P<cmd>PRIVMSG) "
    r"(?P<target>[^ ]+) :(?P<text>.*)$"
)
_TAG_COLOR_RE = re.compile(r"(?:^|;)color=(#[0-9A-Fa-f]{6})")


@dataclass(frozen=True)
class ChatMessage:
    user: str
    text: str
    ts: float
    color: str = "#ffffff"
    channel: str = ""


def _parse_tags(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not raw:
        return out
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
        elif part:
            out[part] = ""
    return out


def _display_name(tags: dict[str, str], login: str) -> str:
    name = (tags.get("display-name") or "").strip()
    return name or login


class TwitchChatClient:
    """Background IRC reader. Thread-safe message buffer + callbacks."""

    def __init__(
        self,
        on_message: Callable[[ChatMessage], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ):
        self.on_message = on_message
        self.on_status = on_status
        self._channel = ""
        self._wanted = ""
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._messages: Deque[ChatMessage] = deque(maxlen=MAX_MESSAGES)
        self._status = "idle"
        self._thread = threading.Thread(target=self._run, name="twitch-irc",
                                        daemon=True)
        self._thread.start()

    # -- public ------------------------------------------------------------
    def set_channel(self, channel: str) -> None:
        """Follow ``channel`` (login, no #). Empty string disconnects."""
        login = (channel or "").strip().lstrip("#").lower()
        with self._lock:
            if login == self._wanted:
                return
            self._wanted = login
            if login != self._channel:
                self._messages.clear()
        self._set_status("switching" if login else "idle")
        self._wake.set()

    def channel(self) -> str:
        with self._lock:
            return self._channel

    def messages(self) -> list[ChatMessage]:
        with self._lock:
            return list(self._messages)

    def status(self) -> str:
        return self._status

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=3.0)

    # -- internals ---------------------------------------------------------
    def _set_status(self, text: str) -> None:
        self._status = text
        if self.on_status:
            try:
                self.on_status(text)
            except Exception:
                log.debug("chat status callback failed", exc_info=True)

    def _push(self, msg: ChatMessage) -> None:
        with self._lock:
            self._messages.append(msg)
        if self.on_message:
            try:
                self.on_message(msg)
            except Exception:
                log.debug("chat message callback failed", exc_info=True)

    def _run(self) -> None:
        backoff = RECONNECT_MIN
        while not self._stop.is_set():
            with self._lock:
                wanted = self._wanted
            if not wanted:
                self._channel = ""
                self._set_status("idle")
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            try:
                self._session(wanted)
                backoff = RECONNECT_MIN
            except Exception as e:
                log.info("twitch chat session ended: %s", e)
                self._set_status(f"reconnect ({e})")
                # Cap wait but wake early if channel changes / stop.
                self._wake.wait(timeout=backoff)
                self._wake.clear()
                backoff = min(RECONNECT_MAX, backoff * 1.7)

    def _session(self, channel: str) -> None:
        nick = f"justinfan{random.randint(10000, 99999)}"
        self._set_status(f"connecting #{channel}")
        sock = socket.create_connection((IRC_HOST, IRC_PORT), timeout=10.0)
        sock.settimeout(RECV_TIMEOUT)
        try:
            self._send(sock, "CAP REQ :twitch.tv/tags twitch.tv/commands")
            self._send(sock, f"NICK {nick}")
            self._send(sock, f"USER {nick} 8 * :{nick}")
            self._send(sock, f"JOIN #{channel}")
            with self._lock:
                self._channel = channel
            self._set_status(f"live #{channel}")
            buf = ""
            while not self._stop.is_set():
                with self._lock:
                    if self._wanted != channel:
                        break
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise ConnectionError("IRC connection closed")
                buf += chunk.decode("utf-8", errors="replace")
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    self._handle_line(sock, line, channel)
        finally:
            try:
                sock.close()
            except Exception:
                pass
            with self._lock:
                if self._channel == channel:
                    self._channel = ""

    def _send(self, sock: socket.socket, line: str) -> None:
        sock.sendall((line + "\r\n").encode("utf-8"))

    def _handle_line(self, sock: socket.socket, line: str, channel: str) -> None:
        if not line:
            return
        if line.startswith("PING "):
            self._send(sock, "PONG " + line[5:])
            return
        m = _PRIVMSG_RE.match(line)
        if not m:
            return
        tags = _parse_tags(m.group("tags") or "")
        prefix = m.group("prefix") or ""
        login = prefix.split("!", 1)[0].lower() if prefix else "unknown"
        text = m.group("text") or ""
        color = tags.get("color") or "#aaaaaa"
        if not color.startswith("#"):
            color = "#aaaaaa"
        msg = ChatMessage(
            user=_display_name(tags, login),
            text=text,
            ts=time.time(),
            color=color,
            channel=channel,
        )
        self._push(msg)
