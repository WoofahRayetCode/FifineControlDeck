"""Action engine: catalog integrity, icon mapping, hotkey parsing."""
import pytest

from fifine_deck import actions
from fifine_deck.model import Action


def test_catalog_types_are_known():
    catalog = {actions.catalog_entry_type(e)
               for _, entries in actions.get_action_catalog() for e in entries}
    assert catalog <= set(actions.ACTION_TYPES)
    assert "multi" in actions.ACTION_TYPES
    assert "" not in catalog
    assert "play_sound" in catalog


def test_system_catalog_has_monitor_metric_presets():
    system = dict(actions.get_action_catalog())["System"]
    labels = [actions.catalog_entry_label(e) for e in system]
    assert "System monitor" in labels
    for want in ("GPU usage", "GPU VRAM", "GPU wattage", "GPU temp",
                 "iGPU usage", "iGPU VRAM", "iGPU wattage", "iGPU temp",
                 "CPU wattage", "CPU temp", "RAM usage", "Game process RAM"):
        assert want in labels
    metrics = {
        actions.catalog_entry_params(e).get("metric")
        for e in system if actions.catalog_entry_params(e)
    }
    assert metrics == {
        "gpu", "vram", "gpupower", "gputemp",
        "igpu", "igpuvram", "igpupower", "igputemp",
        "cpupower", "cputemp", "ram", "procram",
    }


def test_twitch_catalog_has_create_clip_chip():
    twitch = dict(actions.get_action_catalog())["Twitch"]
    labels = [actions.catalog_entry_label(e) for e in twitch]
    assert "Create clip" in labels
    assert "Chatterino command" in labels
    clip = next(e for e in twitch
                if actions.catalog_entry_label(e) == "Create clip")
    assert actions.catalog_entry_type(clip) == "chatterino"
    assert actions.catalog_entry_params(clip).get("command") == "/clip"
    icon, label = actions.default_icon_for(
        Action("chatterino", {"command": "/clip"}))
    assert label == "Clip" and icon == "camera"


def test_obs_catalog_has_stream_presets():
    obs = dict(actions.get_action_catalog())["OBS"]
    labels = [actions.catalog_entry_label(e) for e in obs]
    for want in ("Go Live", "End Stream", "Record", "Stop Record",
                 "Starting Soon", "BRB", "Live", "Game Capture", "Mic Mute"):
        assert want in labels
    go = next(e for e in obs if actions.catalog_entry_label(e) == "Go Live")
    assert actions.catalog_entry_params(go) == {"cmd": "start"}
    icon, label = actions.default_icon_for(
        Action("obs_streaming", {"cmd": "start"}))
    assert label == "Go Live" and icon == "web"
    icon, label = actions.default_icon_for(
        Action("obs_scene", {"scene": "Starting Soon"}))
    assert label == "Soon"


def test_soundboard_catalog_has_meme_clips():
    catalog = dict(actions.get_action_catalog())
    assert "Soundboard" in catalog
    board = catalog["Soundboard"]
    labels = [actions.catalog_entry_label(e) for e in board]
    assert labels[0] == "All sounds folder"
    assert actions.catalog_entry_type(board[0]) == "open_folder"
    assert actions.catalog_entry_params(board[0]).get("preset") == "soundboard"
    assert "Random Funny" in labels
    assert "Bruh" in labels
    assert "Vine boom" in labels
    # Source tags stripped for the chip face / sidebar.
    assert not any("MyInstants" in lab for lab in labels)
    clips = {
        actions.catalog_entry_params(e).get("clip")
        for e in board if actions.catalog_entry_type(e) == "play_sound"
    }
    assert "bruh" in clips and "vine_boom" in clips
    assert "random_funny" in clips
    types = {actions.catalog_entry_type(e) for e in board}
    assert types == {"open_folder", "play_sound"}


def test_catalog_drag_round_trip_presets():
    entry = {"type": "monitor", "label": "GPU usage",
             "params": {"metric": "gpu"}}
    token = actions.encode_catalog_drag(entry)
    assert token == "monitor?metric=gpu"
    assert actions.parse_catalog_drag(token) == ("monitor", {"metric": "gpu"})
    assert actions.parse_catalog_drag("volume") == ("volume", {})
    assert actions.encode_catalog_drag("volume") == "volume"


def test_default_icon_for_every_type():
    for t in actions.ACTION_TYPES:
        icon, label = actions.default_icon_for(Action(t, {}))
        assert isinstance(icon, str) and isinstance(label, str)


def test_default_icon_subcommand_variants():
    assert actions.default_icon_for(Action("volume", {"cmd": "down"}))[0] == "volume_down"
    assert actions.default_icon_for(Action("volume", {"cmd": "mute"}))[0] == "mute"
    assert actions.default_icon_for(Action("media", {"cmd": "next"}))[0] == "next"
    assert actions.default_icon_for(Action("brightness", {"mode": "down"}))[0] == "brightness_down"


def test_ydotool_keycodes():
    assert actions._ydotool_keycodes("ctrl+shift+m") == [29, 42, 50]
    assert actions._ydotool_keycodes("a") == [30]
    assert actions._ydotool_keycodes("totallyboguskey") is None



def test_environment_summary_shape():
    s = actions.environment_summary()
    assert "session=" in s and "audio=" in s and "keytool=" in s


def test_execute_none_is_safe():
    # a 'none' action must be a no-op and never raise
    actions.execute(Action("none", {}))
