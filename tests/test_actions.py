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
                 "CPU wattage", "CPU temp", "RAM usage", "Game process RAM",
                 "FPS (MangoHud)"):
        assert want in labels
    metrics = {
        actions.catalog_entry_params(e).get("metric")
        for e in system if actions.catalog_entry_params(e)
    }
    assert metrics == {
        "gpu", "vram", "gpupower", "gputemp",
        "igpu", "igpuvram", "igpupower", "igputemp",
        "cpupower", "cputemp", "ram", "procram", "fps",
    }


def test_power_catalog_has_kde_profile_chips():
    catalog = dict(actions.get_action_catalog())
    assert "Power" in catalog
    power = catalog["Power"]
    labels = [actions.catalog_entry_label(e) for e in power]
    for want in ("Performance", "Balanced", "Power Saver",
                 "Cycle power profile", "Power profile"):
        assert want in labels
    perf = next(e for e in power
                if actions.catalog_entry_label(e) == "Performance")
    assert actions.catalog_entry_type(perf) == "power_profile"
    assert actions.catalog_entry_params(perf) == {"profile": "performance"}
    icon, label = actions.default_icon_for(
        Action("power_profile", {"profile": "performance"}))
    assert label == "Perf" and icon == "brightness_up"
    icon, label = actions.default_icon_for(
        Action("power_profile", {"profile": "power-saver"}))
    assert label == "Saver" and icon == "brightness_down"


def test_power_catalog_has_system_power_chips():
    power = dict(actions.get_action_catalog())["Power"]
    labels = [actions.catalog_entry_label(e) for e in power]
    for want in ("Sleep", "Hibernate", "Shutdown"):
        assert want in labels
    sleep = next(e for e in power if actions.catalog_entry_label(e) == "Sleep")
    assert actions.catalog_entry_type(sleep) == "system_power"
    assert actions.catalog_entry_params(sleep) == {"cmd": "sleep"}
    icon, label = actions.default_icon_for(
        Action("system_power", {"cmd": "shutdown"}))
    assert label == "Shutdown" and icon == "power"
    icon, label = actions.default_icon_for(
        Action("system_power", {"cmd": "hibernate"}))
    assert label == "Hibernate" and icon == "lock"


def test_twitch_catalog_has_create_clip_chip():
    twitch = dict(actions.get_action_catalog())["Twitch"]
    labels = [actions.catalog_entry_label(e) for e in twitch]
    assert "Twitch chat" in labels
    assert "Twitch ad countdown" in labels
    assert "Snooze ad" in labels
    assert "Create clip" in labels
    assert "Chatterino command" in labels
    chat = next(e for e in twitch
                if actions.catalog_entry_label(e) == "Twitch chat")
    assert actions.catalog_entry_type(chat) == "show_twitch_chat"
    ad = next(e for e in twitch
              if actions.catalog_entry_label(e) == "Twitch ad countdown")
    assert actions.catalog_entry_type(ad) == "monitor"
    assert actions.catalog_entry_params(ad).get("metric") == "twitchad"
    snooze = next(e for e in twitch
                  if actions.catalog_entry_label(e) == "Snooze ad")
    assert actions.catalog_entry_type(snooze) == "twitch_snooze_ad"
    clip = next(e for e in twitch
                if actions.catalog_entry_label(e) == "Create clip")
    assert actions.catalog_entry_type(clip) == "chatterino"
    assert actions.catalog_entry_params(clip).get("command") == "/clip"
    icon, label = actions.default_icon_for(
        Action("chatterino", {"command": "/clip"}))
    assert label == "Clip" and icon == "camera"
    icon, label = actions.default_icon_for(Action("show_twitch_chat", {}))
    assert label == "Chat" and icon == "web"


def test_obs_catalog_has_stream_presets():
    obs = dict(actions.get_action_catalog())["OBS"]
    labels = [actions.catalog_entry_label(e) for e in obs]
    for want in ("Go Live", "End Stream", "Record", "Stop Record",
                 "Starting Soon", "BRB", "Live", "Game Capture",
                 "BRB Source", "Mic Mute"):
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
