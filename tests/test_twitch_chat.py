"""Twitch chat page mode: IRC parse helpers + page schema."""
from __future__ import annotations

import time

from fifine_deck.controller import CHAT_BACK_DOUBLE_PRESS, DeckController
from fifine_deck.model import (PAGE_DISPLAY_KEYS, PAGE_DISPLAY_PHOTO,
                               PAGE_DISPLAY_TWITCH_CHAT, DeckConfig, Page)
from fifine_deck.twitch_chat import _PRIVMSG_RE, _parse_tags, _display_name


def test_page_twitch_chat_roundtrip():
    page = Page(name="Chat", display_mode=PAGE_DISPLAY_TWITCH_CHAT,
                chat_channel="CoolStreamer")
    data = page.to_dict()
    assert data["display_mode"] == PAGE_DISPLAY_TWITCH_CHAT
    assert data["chat_channel"] == "coolstreamer"
    back = Page.from_dict(data)
    assert back.is_twitch_chat()
    assert back.chat_channel == "coolstreamer"


def test_page_default_is_keys():
    page = Page.from_dict({"name": "Main", "keys": {}})
    assert page.display_mode == PAGE_DISPLAY_KEYS
    assert not page.is_twitch_chat()
    assert "display_mode" not in page.to_dict()


def test_page_unknown_mode_falls_back():
    page = Page.from_dict({"display_mode": "hologram", "chat_channel": "#X"})
    assert page.display_mode == PAGE_DISPLAY_KEYS
    assert page.chat_channel == "x"


def test_privmsg_parse_with_tags():
    line = ("@badge-info=;badges=;color=#1E90FF;display-name=Alice;"
            "user-type= :alice!alice@alice.tmi.twitch.tv PRIVMSG #channel "
            ":hello there")
    m = _PRIVMSG_RE.match(line)
    assert m is not None
    assert m.group("text") == "hello there"
    tags = _parse_tags(m.group("tags"))
    assert tags.get("color") == "#1E90FF"
    assert _display_name(tags, "alice") == "Alice"


def test_privmsg_parse_without_tags():
    line = ":bob!bob@bob.tmi.twitch.tv PRIVMSG #channel :yo"
    m = _PRIVMSG_RE.match(line)
    assert m is not None
    assert m.group("text") == "yo"


def test_exit_twitch_chat_prefers_previous_keys_page():
    cfg = DeckConfig()
    profile = cfg.profiles[0]
    profile.pages = [
        Page(name="Chips", display_mode=PAGE_DISPLAY_KEYS),
        Page(name="Chat", display_mode=PAGE_DISPLAY_TWITCH_CHAT,
             chat_channel="shroud"),
    ]
    ctrl = DeckController(cfg)
    try:
        ctrl.page_index = 1
        assert ctrl.page().is_twitch_chat()
        ctrl.exit_twitch_chat_page()
        assert ctrl.page_index == 0
        assert not ctrl.page().is_twitch_chat()
    finally:
        ctrl.stop()


def test_show_twitch_chat_opens_existing_page():
    cfg = DeckConfig()
    profile = cfg.profiles[0]
    profile.pages = [
        Page(name="Chips", display_mode=PAGE_DISPLAY_KEYS),
        Page(name="Chat", display_mode=PAGE_DISPLAY_TWITCH_CHAT,
             chat_channel="shroud"),
    ]
    ctrl = DeckController(cfg)
    try:
        ctrl.page_index = 0
        ctrl.show_twitch_chat("shroud")
        assert ctrl.page_index == 1
        assert ctrl.page().is_twitch_chat()
        assert ctrl.page().chat_channel == "shroud"
    finally:
        ctrl.stop()


def test_show_twitch_chat_creates_page_when_missing():
    cfg = DeckConfig()
    profile = cfg.profiles[0]
    profile.pages = [Page(name="Chips", display_mode=PAGE_DISPLAY_KEYS)]
    ctrl = DeckController(cfg)
    try:
        ctrl.show_twitch_chat("#CoolStreamer")
        assert len(profile.pages) == 2
        assert ctrl.page().is_twitch_chat()
        assert ctrl.page().chat_channel == "coolstreamer"
        assert "coolstreamer" in ctrl.page().name.lower()
    finally:
        ctrl.stop()


def test_chat_tile_marquee_changes_with_scroll():
    from fifine_deck import rendering
    long_text = "this is a deliberately long chat message for marquee"
    a = rendering.render_chat_tile(100, user="alice", text=long_text, scroll_px=0)
    b = rendering.render_chat_tile(100, user="alice", text=long_text, scroll_px=40)
    assert a.size == (100, 100)
    assert a.tobytes() != b.tobytes()
    tw, vw, _ = rendering.chat_body_metrics(100, long_text)
    assert tw > vw


def test_chat_tile_short_text_ignores_scroll():
    from fifine_deck import rendering
    a = rendering.render_chat_tile(100, user="a", text="hi", scroll_px=0)
    b = rendering.render_chat_tile(100, user="a", text="hi", scroll_px=50)
    assert a.tobytes() == b.tobytes()


def test_page_photo_roundtrip(tmp_path):
    img = tmp_path / "cat.png"
    # Minimal valid 1x1 PNG
    img.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
    page = Page(name="Cat", display_mode=PAGE_DISPLAY_PHOTO,
                photo_path=str(img))
    data = page.to_dict()
    assert data["display_mode"] == PAGE_DISPLAY_PHOTO
    assert data["photo_path"] == str(img)
    back = Page.from_dict(data)
    assert back.is_photo() and back.is_special()
    assert back.photo_path == str(img)


def test_slice_photo_tiles(tmp_path):
    from PIL import Image
    from fifine_deck import rendering
    path = tmp_path / "wide.jpg"
    Image.new("RGB", (500, 300), (20, 40, 60)).save(path, quality=90)
    tiles = rendering.slice_photo_tiles(str(path), cols=5, rows=3, key_size=100)
    assert len(tiles) == 15
    assert tiles[1].size == (100, 100)
    badged = rendering.badge_photo_back_tile(tiles[1])
    assert badged.size == (100, 100)
    assert badged.tobytes() != tiles[1].tobytes()


def test_load_page_gif_animation(tmp_path):
    from PIL import Image
    from fifine_deck import rendering
    path = tmp_path / "blink.gif"
    frames = [
        Image.new("RGB", (50, 30), (255, 0, 0)),
        Image.new("RGB", (50, 30), (0, 255, 0)),
        Image.new("RGB", (50, 30), (0, 0, 255)),
    ]
    frames[0].save(
        path, save_all=True, append_images=frames[1:],
        duration=80, loop=0)
    anim = rendering.load_page_gif_animation(
        str(path), cols=5, rows=3, key_size=20)
    assert len(anim) >= 2
    tiles0, delay0 = anim[0]
    assert delay0 >= 40
    assert len(tiles0) == 15
    assert tiles0[1].size == (20, 20)
    # Frames should differ after colour change.
    assert tiles0[1].tobytes() != anim[1][0][1].tobytes()


def test_exit_photo_page_to_keys():
    cfg = DeckConfig()
    profile = cfg.profiles[0]
    profile.pages = [
        Page(name="Chips"),
        Page(name="Cat", display_mode=PAGE_DISPLAY_PHOTO, photo_path="/x.png"),
    ]
    ctrl = DeckController(cfg)
    try:
        ctrl.page_index = 1
        assert ctrl.page().is_photo()
        ctrl.exit_to_keys_page()
        assert ctrl.page_index == 0
        assert not ctrl.page().is_special()
    finally:
        ctrl.stop()


def test_chat_header_double_press_enqueues_exit(monkeypatch):
    cfg = DeckConfig()
    profile = cfg.profiles[0]
    profile.pages = [
        Page(name="Chips"),
        Page(name="Chat", display_mode=PAGE_DISPLAY_TWITCH_CHAT,
             chat_channel="x"),
    ]
    ctrl = DeckController(cfg)
    calls = []
    try:
        ctrl.page_index = 1
        monkeypatch.setattr(ctrl, "_enqueue", lambda fn: calls.append(fn))
        ctrl._chat_header_press()
        assert calls == []
        # Second press inside the double-tap window.
        ctrl._chat_back_last = time.monotonic() - (CHAT_BACK_DOUBLE_PRESS / 2)
        ctrl._chat_header_press()
        assert calls == [ctrl.exit_to_keys_page]
    finally:
        ctrl.stop()
