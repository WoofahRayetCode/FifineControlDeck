"""OBS starter layout for brand-new configs."""
from __future__ import annotations

import os

from fifine_deck.model import DeckConfig
from fifine_deck.starter_layout import _MEME_CLIPS, apply_starter_layout


def test_apply_starter_layout_builds_obs_and_meme_folders():
    cfg = DeckConfig()
    apply_starter_layout(cfg)
    page = cfg.active_profile().pages[0]
    assert page.key(1).label == "Streaming"
    assert page.key(2).label == "Recording"
    assert page.key(3).label == "Scenes"
    assert page.key(4).label == "General"
    assert page.key(5).label == "Memes"
    assert page.key(5).folder is not None
    assert page.key(6).action.params["clip"] == "random_funny"
    assert page.key(7).action.params["clip"] == "random_fart"
    assert page.key(8).action.params["clip"] == "random_music"
    streaming = page.key(1).folder.pages[0].keys
    assert streaming[1].label == "Go Live"
    assert streaming[1].action.params["cmd"] == "start"
    assert streaming[2].label == "End Live"
    scenes = page.key(3).folder.pages[0].keys
    assert scenes[1].action.params["scene"] == "Starting Soon"
    assert scenes[2].action.params["scene"] == "BRB"
    assert scenes[3].action.params["scene"] == "Live"
    assert scenes[4].action.params["scene"] == "Game Capture"
    for idx in (1, 2, 3, 4, 5):
        folder = page.key(idx).folder
        assert folder is not None
        last = max(folder.pages[0].keys)
        assert folder.pages[0].keys[last].action.type == "folder_back"


def test_memes_folder_lists_meme_clips_across_pages():
    cfg = DeckConfig()
    apply_starter_layout(cfg)
    memes = cfg.active_profile().pages[0].key(5).folder
    assert memes is not None
    assert len(memes.pages) >= 2
    found: list[str] = []
    n = len(memes.pages)
    for i, pg in enumerate(memes.pages):
        for idx, kc in sorted(pg.keys.items()):
            if kc.action.type == "play_sound":
                found.append(kc.action.params["clip"])
        # Folders: Next on non-last pages only — never Prev (Back is smart).
        prev = pg.keys.get(13)
        nxt = pg.keys.get(14)
        assert prev is None or prev.is_empty() or prev.action.type != "prev_page"
        if i < n - 1:
            assert nxt is not None and nxt.action.type == "next_page"
        else:
            assert nxt is None or nxt.is_empty() or nxt.action.type != "next_page"
    assert found == [clip for clip, _label in _MEME_CLIPS]
    assert "gawd_dayum" in found
    assert "vine_boom" in found
    assert "multi_yeet" in found
    assert "rizz" in found
    assert "bleep" not in found


def test_apply_page_nav_keys_first_middle_last():
    from fifine_deck.model import Page
    from fifine_deck.starter_layout import apply_page_nav_keys
    pages = [Page(name="A"), Page(name="B"), Page(name="C")]
    apply_page_nav_keys(pages)
    # Root: Next on bottom-right (15), not 14.
    assert pages[0].keys[15].action.type == "next_page"
    assert 14 not in pages[0].keys or pages[0].keys[14].is_empty()
    assert 13 not in pages[0].keys or pages[0].keys[13].is_empty()
    assert pages[1].keys[13].action.type == "prev_page"
    assert pages[1].keys[15].action.type == "next_page"
    assert pages[2].keys[13].action.type == "prev_page"
    assert 15 not in pages[2].keys or pages[2].keys[15].is_empty()


def test_folder_page_nav_has_next_but_never_prev():
    from fifine_deck.model import Action, KeyConfig, Page
    from fifine_deck.starter_layout import apply_page_nav_keys
    pages = [Page(name="A"), Page(name="B"), Page(name="C")]
    # Simulate a leftover Prev from an older layout.
    pages[1].keys[13] = KeyConfig(
        label="Prev", action=Action("prev_page", {}))
    apply_page_nav_keys(pages, in_folder=True)
    for pg in pages:
        prev = pg.keys.get(13)
        assert prev is None or prev.is_empty()
    # Folders keep Next on 14 so Back can own 15.
    assert pages[0].keys[14].action.type == "next_page"
    assert pages[1].keys[14].action.type == "next_page"
    assert 14 not in pages[2].keys or pages[2].keys[14].is_empty()


def test_build_soundboard_folder_includes_all_bundled_clips():
    from fifine_deck import sounds
    from fifine_deck.starter_layout import build_soundboard_folder
    kc = build_soundboard_folder(name="Memes")
    assert kc.action.type == "open_folder"
    assert kc.folder is not None
    found = [
        k.action.params["clip"]
        for pg in kc.folder.pages
        for k in pg.keys.values()
        if k.action.type == "play_sound"
    ]
    expect = [c["name"] for c in sounds.list_clips() if c.get("path")]
    assert found == expect
    assert len(kc.folder.pages) >= 2


def test_apply_page_nav_preserves_user_keys_on_nav_slots():
    from fifine_deck.model import Action, KeyConfig, Page
    from fifine_deck.starter_layout import apply_page_nav_keys
    pages = [Page(name="A"), Page(name="B")]
    # User key on the root Next slot (15) must not be overwritten.
    pages[0].keys[15] = KeyConfig(
        label="Mine", action=Action("hotkey", {"keys": "a"}))
    apply_page_nav_keys(pages)
    assert pages[0].keys[15].action.type == "hotkey"
    assert pages[0].keys[15].label == "Mine"


def test_apply_page_nav_clears_duplicate_next_on_old_slot():
    """Root Next lives on 15; a leftover Next on 14 must be cleared."""
    from fifine_deck.model import Action, KeyConfig, Page
    from fifine_deck.starter_layout import apply_page_nav_keys
    pages = [Page(name="A"), Page(name="B")]
    pages[0].keys[14] = KeyConfig(
        label="Next", action=Action("next_page", {}))
    pages[0].keys[15] = KeyConfig(
        label="Next", action=Action("next_page", {}))
    apply_page_nav_keys(pages)
    assert pages[0].keys[15].action.type == "next_page"
    assert pages[0].keys.get(14) is None or pages[0].keys[14].is_empty()


def test_general_folder_has_stream_record_and_multi():
    cfg = DeckConfig()
    apply_starter_layout(cfg)
    general = cfg.active_profile().pages[0].key(4).folder
    assert general is not None
    keys = general.pages[0].keys
    assert keys[1].action.type == "obs_mute"
    assert keys[2].action.type == "obs_streaming"
    assert keys[2].label == "Go Live"
    assert keys[3].action.type == "obs_recording"
    assert keys[4].action.type == "multi"
    steps = keys[4].action.params["steps"]
    assert steps[0]["action"]["type"] == "obs_streaming"
    assert steps[1]["action"]["type"] == "obs_recording"
    assert keys[5].action.type == "multi"
    assert keys[6].action.type == "chatterino"
    assert keys[6].action.params.get("command") == "/clip"


def test_load_missing_applies_starter(tmp_path):
    p = str(tmp_path / "fresh.json")
    cfg = DeckConfig.load(p)
    assert os.path.exists(p)
    page = cfg.active_profile().pages[0]
    assert page.key(1).label == "Streaming"
    assert page.key(3).label == "Scenes"
    assert page.key(5).label == "Memes"


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
