"""Twitch Helix helper unit tests (no live network)."""
import json
import urllib.request
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


def test_parse_twitch_time_unix_and_rfc3339():
    assert twitch._parse_twitch_time(1728825458) == 1728825458.0
    assert twitch._parse_twitch_time("1728825458") == 1728825458.0
    assert twitch._parse_twitch_time(0) is None
    assert twitch._parse_twitch_time("60") is None  # duration, not epoch
    ts = twitch._parse_twitch_time("2024-03-01T12:00:00Z")
    assert ts is not None and ts > 1_700_000_000


def test_fmt_ad_countdown():
    assert twitch.fmt_ad_countdown(0) == "NOW"
    assert twitch.fmt_ad_countdown(65) == "1:05"
    assert twitch.fmt_ad_countdown(600) == "10m"


def test_open_url_nonblocking_uses_xdg(monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            seen["args"] = args

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/xdg-open")
    monkeypatch.setattr("subprocess.Popen", FakePopen)
    twitch.open_url_nonblocking("https://example.com/x")
    assert seen["args"] == ["/usr/bin/xdg-open", "https://example.com/x"]


def test_device_login_session_cancel(monkeypatch):
    """finish() must return promptly when cancel() is called."""
    import threading
    import time as _time

    session = twitch.DeviceLoginSession("cid", "sec", timeout=30.0)
    session._device_code = "dev"
    session.user_code = "ABCD"
    session._interval = 0.2

    def boom(*a, **k):
        raise AssertionError("should cancel before polling Twitch")

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    def cancel_soon():
        _time.sleep(0.05)
        session.cancel()

    threading.Thread(target=cancel_soon, daemon=True).start()
    t0 = _time.monotonic()
    ok, msg, *_ = session.finish()
    assert not ok and "cancel" in msg.lower()
    assert _time.monotonic() - t0 < 2.0


def test_device_login_begin_parses_response(monkeypatch):
    def fake_http(method, url, headers=None, body=None):
        assert "oauth2/device" in url
        return {
            "device_code": "dev123",
            "user_code": "XY-Z9",
            "verification_uri": "https://www.twitch.tv/activate",
            "verification_uri_complete":
                "https://www.twitch.tv/activate?device-code=XY-Z9",
            "expires_in": 600,
            "interval": 5,
        }

    monkeypatch.setattr(twitch, "_http_json", fake_http)
    session = twitch.DeviceLoginSession("cid", "sec")
    url = session.begin()
    assert "twitch.tv/activate" in url
    assert session.user_code == "XY-Z9"
    assert session._device_code == "dev123"


def test_get_ad_schedule_uses_user_token(monkeypatch):
    import time as _time
    twitch.invalidate()
    monkeypatch.setattr(twitch, "_creds_provider", lambda: ("cid", "sec"))
    monkeypatch.setattr(
        twitch, "get_user_access_token",
        lambda: ("uat", "99", "streamer"))
    next_at = _time.time() + 120

    def fake_http(method, url, headers=None, body=None):
        assert "channels/ads" in url
        assert headers["Authorization"] == "Bearer uat"
        return {"data": [{
            "next_ad_at": next_at,
            "duration": 60,
            "snooze_count": 2,
            "preroll_free_time": 0,
            "last_ad_at": 0,
            "snooze_refresh_at": 0,
        }]}

    monkeypatch.setattr(twitch, "_http_json", fake_http)
    sched = twitch.get_ad_schedule()
    assert sched is not None
    assert sched.duration == 60
    assert sched.snooze_count == 2
    assert sched.seconds_until_ad is not None
    assert 100 <= sched.seconds_until_ad <= 130


def test_get_ad_schedule_without_login_is_none(monkeypatch):
    twitch.invalidate()
    monkeypatch.setattr(twitch, "get_user_access_token", lambda: None)
    assert twitch.get_ad_schedule() is None


def test_snooze_next_ad_ok(monkeypatch):
    import time as _time
    twitch.invalidate()
    monkeypatch.setattr(twitch, "_creds_provider", lambda: ("cid", "sec"))
    monkeypatch.setattr(
        twitch, "get_user_access_token",
        lambda: ("uat", "99", "streamer"))
    next_at = _time.time() + 300

    def fake_http(method, url, headers=None, body=None):
        assert method == "POST"
        assert "snooze" in url
        return {"data": [{
            "next_ad_at": next_at,
            "snooze_count": 1,
            "snooze_refresh_at": next_at + 3600,
        }]}

    monkeypatch.setattr(twitch, "_http_json", fake_http)
    ok, msg = twitch.snooze_next_ad()
    assert ok and "snooze" in msg.lower()
