"""Twitch Helix helper unit tests (no live network)."""
import json
from datetime import datetime, timezone, timedelta

import pytest

from fifine_deck import twitch


def test_stream_info_uptime_seconds():
    started = (datetime.now(timezone.utc) - timedelta(minutes=10)
               ).strftime("%Y-%m-%dT%H:%M:%SZ")
    info = twitch.StreamInfo(live=True, login="x", started_at=started)
    assert info.uptime_seconds is not None
    assert 9 * 60 <= info.uptime_seconds <= 11 * 60
    assert twitch.StreamInfo(live=False, login="x").uptime_seconds is None


def test_get_stream_uses_cache(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(login, client_id, client_secret):
        calls["n"] += 1
        return twitch.StreamInfo(live=True, login=login, viewer_count=9,
                                 started_at="2026-01-01T00:00:00Z")

    twitch.invalidate()
    monkeypatch.setattr(twitch, "_creds_provider",
                        lambda: ("id", "secret"))
    monkeypatch.setattr(twitch, "_fetch_stream", fake_fetch)
    a = twitch.get_stream("Demo")
    b = twitch.get_stream("demo")  # normalized + cached
    assert a is not None and a.viewer_count == 9
    assert b is not None and b.viewer_count == 9
    assert calls["n"] == 1


def test_get_stream_without_creds_is_none(monkeypatch):
    twitch.invalidate()
    monkeypatch.setattr(twitch, "_creds_provider", lambda: ("", ""))
    assert twitch.get_stream("x") is None
    monkeypatch.setattr(twitch, "_creds_provider", None)
    assert twitch.get_stream("x") is None


def test_ping_requires_both(monkeypatch):
    ok, msg = twitch.ping("", "secret")
    assert not ok and "required" in msg.lower()
    ok, msg = twitch.ping("id", "")
    assert not ok and "required" in msg.lower()


def test_app_access_token_caches(monkeypatch):
    twitch.invalidate()
    bodies = []

    def fake_http(method, url, headers=None, body=None):
        bodies.append((method, url, body))
        return {"access_token": "tok", "expires_in": 3600}

    monkeypatch.setattr(twitch, "_http_json", fake_http)
    assert twitch._app_access_token("cid", "sec") == "tok"
    assert twitch._app_access_token("cid", "sec") == "tok"  # cached
    assert len(bodies) == 1


def test_fetch_stream_parses_helix(monkeypatch):
    def fake_http(method, url, headers=None, body=None):
        if "oauth2/token" in url:
            return {"access_token": "tok", "expires_in": 3600}
        return {"data": [{
            "user_login": "demo",
            "viewer_count": 42,
            "started_at": "2026-01-01T12:00:00Z",
            "title": "Hi",
            "game_name": "Music",
        }]}

    twitch.invalidate()
    monkeypatch.setattr(twitch, "_http_json", fake_http)
    info = twitch._fetch_stream("demo", "cid", "sec")
    assert info.live and info.viewer_count == 42
    assert info.game_name == "Music"


def test_fetch_stream_empty_is_offline(monkeypatch):
    def fake_http(method, url, headers=None, body=None):
        if "oauth2/token" in url:
            return {"access_token": "tok", "expires_in": 3600}
        return {"data": []}

    twitch.invalidate()
    monkeypatch.setattr(twitch, "_http_json", fake_http)
    info = twitch._fetch_stream("demo", "cid", "sec")
    assert not info.live and info.login == "demo"
