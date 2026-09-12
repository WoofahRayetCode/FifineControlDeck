"""Bundled sound clips and play_sound resolution."""
from __future__ import annotations

import os

from fifine_deck import sounds


def test_bundled_clips_are_myinstants_only():
    clips = sounds.list_clips()
    assert len(clips) >= 20
    names = {c["name"] for c in clips}
    for need in ("bruh", "vine_boom", "gawd_dayum", "oh_my_god", "brain_fart",
                 "bark_fart", "wtf_boom", "multi_yeet", "tiktok_india", "rizz",
                 "emotional_damage", "shocked_sound", "asian_meme_huh",
                 "french_meme_song", "du_bist_gut_genug",
                 "directed_by_robert_weide", "okay_lets_go", "rat_dance_music",
                 "metal_gear_alert", "gas_gas_gas", "wet_fart",
                 "gegagedigedagedago", "sad_violin", "e_meme", "deja_vu_fade",
                 "samsung_notification", "hello_meme", "ah_shit_here_we_go_again",
                 "shooting_stars", "doom_music", "oblivion_npc_theme", "meme_67",
                 "butter_dog", "arabic_nokia"):
        assert need in names
        path = sounds.resolve_clip(need)
        assert path and os.path.isfile(path)
    # Classic synthesised library is gone
    for gone in ("bleep", "airhorn", "fart_squeak", "circus", "elevator",
                 "kazoo", "laugh", "fart_gamecube", "what_the_fuck"):
        assert gone not in names
    for c in clips:
        assert "(MyInstants)" in c["label"], c["name"]


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
    monkeypatch.setattr(sounds.shutil, "which",
                        lambda c: "/usr/bin/pw-play" if c == "pw-play" else None)
    assert sounds.play("bruh", volume=50) is True
    assert calls and calls[0][0] == "pw-play"
    assert any("bruh.wav" in a for a in calls[0])


def test_play_targets_sink_and_dual_plays(monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append(list(argv))

    monkeypatch.setattr(sounds.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(sounds.shutil, "which",
                        lambda c: "/usr/bin/pw-play" if c == "pw-play" else None)
    assert sounds.play("bruh", volume=40, sink="Soundboard",
                       also_default=True) is True
    assert len(calls) == 2
    assert "--target" in calls[0] and "Soundboard" in calls[0]
    assert "--target" not in calls[1]  # default output has no --target


def test_player_cmd_paplay_device(monkeypatch):
    monkeypatch.setattr(sounds.shutil, "which",
                        lambda c: "/bin/paplay" if c == "paplay" else None)
    argv = sounds._player_cmd("/tmp/x.wav", 80, sink="foo")
    assert argv[:1] == ["paplay"]
    assert "--device=foo" in argv


def test_list_sinks_parses_pactl_json(monkeypatch):
    import json
    payload = json.dumps([
        {"name": "a_sink", "description": "A Sink"},
        {"name": "b_sink", "properties": {"node.description": "B"}},
    ])
    monkeypatch.setattr(sounds.shutil, "which",
                        lambda c: "/bin/pactl" if c == "pactl" else None)

    def fake_check_output(argv, **kw):
        assert argv[:3] == ["pactl", "-f", "json"]
        return payload

    monkeypatch.setattr(sounds.subprocess, "check_output", fake_check_output)
    assert sounds.list_sinks() == [("a_sink", "A Sink"), ("b_sink", "B")]


def test_play_without_player_logs(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(sounds.shutil, "which", lambda c: None)
    with caplog.at_level(logging.WARNING, logger="fifine_deck.sounds"):
        assert sounds.play("bruh") is False
    assert any("pw-play" in r.message for r in caplog.records)
