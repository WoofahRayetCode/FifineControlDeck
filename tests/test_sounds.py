"""Bundled sound clips and play_sound resolution."""
from __future__ import annotations

import os

from fifine_deck import sounds


def test_bundled_clips_exist():
    clips = sounds.list_clips()
    assert len(clips) >= 20
    names = {c["name"] for c in clips}
    for need in ("bleep", "airhorn", "fart_squeak", "fart_wet", "circus",
                 "elevator", "kazoo", "sad_trombone"):
        assert need in names
        path = sounds.resolve_clip(need)
        assert path and os.path.isfile(path)


def test_random_pools():
    for clip in ("random", "random_funny", "random_fart", "random_music"):
        path = sounds.resolve_clip(clip)
        assert path and os.path.isfile(path)


def test_custom_file(tmp_path):
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"RIFF")  # existence check only; player not invoked
    assert sounds.resolve_clip("custom", str(wav)) == str(wav)
    assert sounds.resolve_clip("custom", "") is None


def test_play_starts_player(monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append(argv)

    monkeypatch.setattr(sounds.subprocess, "Popen", FakePopen)
    # Force a known player path
    monkeypatch.setattr(sounds.shutil, "which",
                        lambda c: "/usr/bin/pw-play" if c == "pw-play" else None)
    assert sounds.play("bleep", volume=50) is True
    assert calls and calls[0][0] == "pw-play"
    assert any("bleep.wav" in a for a in calls[0])


def test_play_without_player_logs(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(sounds.shutil, "which", lambda c: None)
    with caplog.at_level(logging.WARNING, logger="fifine_deck.sounds"):
        assert sounds.play("bleep") is False
    assert any("pw-play" in r.message for r in caplog.records)
