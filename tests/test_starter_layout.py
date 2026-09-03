"""OBS starter layout for brand-new configs."""
from __future__ import annotations

import os

from fifine_deck.model import DeckConfig
from fifine_deck.starter_layout import apply_starter_layout


def test_apply_starter_layout_builds_obs_and_sound_folders():
    cfg = DeckConfig()
    apply_starter_layout(cfg)
    page = cfg.active_profile().pages[0]
    assert page.key(1).action.type == "open_folder"
    assert page.key(1).folder is not None
    assert page.key(1).label == "Streaming"
    assert page.key(2).label == "Recording"
    assert page.key(3).label == "General"
    assert page.key(4).label == "Sounds"
    assert page.key(4).folder is not None
    # Soundboard has play_sound keys + Back
    snd = page.key(4).folder.pages[0].keys
    assert snd[1].action.type == "play_sound"
    assert snd[1].action.params["clip"] == "bleep"
    assert snd[3].action.params["clip"] == "fart_squeak"
    assert snd[14].action.params["clip"] == "random_funny"
    assert page.key(5).action.type == "play_sound"
    assert page.key(5).action.params["clip"] == "random_fart"
    # Back key on last slot inside each folder
    for idx in (1, 2, 3, 4):
        folder = page.key(idx).folder
        assert folder is not None
        last = max(folder.pages[0].keys)
        assert folder.pages[0].keys[last].action.type == "folder_back"


def test_general_folder_has_stream_record_and_multi():
    cfg = DeckConfig()
    apply_starter_layout(cfg)
    general = cfg.active_profile().pages[0].key(3).folder
    assert general is not None
    keys = general.pages[0].keys
    assert keys[1].action.type == "obs_mute"
    assert keys[2].action.type == "obs_streaming"
    assert keys[3].action.type == "obs_recording"
    assert keys[4].action.type == "multi"
    steps = keys[4].action.params["steps"]
    assert steps[0]["action"]["type"] == "obs_streaming"
    assert steps[1]["action"]["type"] == "obs_recording"
    assert keys[5].action.type == "multi"


def test_load_missing_applies_starter(tmp_path):
    p = str(tmp_path / "fresh.json")
    cfg = DeckConfig.load(p)
    assert os.path.exists(p)
    page = cfg.active_profile().pages[0]
    assert page.key(1).label == "Streaming"
    assert page.key(2).label == "Recording"
    assert page.key(3).label == "General"
    assert page.key(4).label == "Sounds"


def test_existing_config_is_not_rewritten(tmp_path):
    p = str(tmp_path / "mine.json")
    cfg = DeckConfig()
    cfg.profiles[0].name = "Mine"
    cfg.profiles[0].pages[0].key(1).label = "Only"
    cfg.save(p)
    loaded = DeckConfig.load(p)
    assert loaded.profiles[0].name == "Mine"
    assert loaded.profiles[0].pages[0].key(1).label == "Only"
    assert 2 not in loaded.profiles[0].pages[0].keys \
        or loaded.profiles[0].pages[0].key(2).is_empty()
