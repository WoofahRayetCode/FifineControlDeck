"""Starter layout for a brand-new configuration.

Applied only when DeckConfig.load() creates a fresh config (missing or
unreadable file). Existing user configs are never rewritten.

Includes OBS control folders plus a comedy soundboard (bleeps, farts,
unfitting jingles) for the Play sound action.
"""
from __future__ import annotations

from . import assets
from .device import DEVICE_PROFILE
from .model import Action, DeckConfig, Folder, KeyConfig, Page


def _key(label: str, icon: str, action: Action, *,
         bg: str = "#101020") -> KeyConfig:
    return KeyConfig(
        label=label,
        icon=assets.library_ref(icon) if icon else "",
        bg_color=bg,
        action=action,
    )


def _snd(label: str, clip: str, *, icon: str = "play",
         bg: str = "#2a1a30", volume: str = "85") -> KeyConfig:
    return _key(label, icon, Action("play_sound", {
        "clip": clip, "file": "", "volume": volume,
    }), bg=bg)


def _back_key() -> KeyConfig:
    return KeyConfig(
        label="Back",
        icon=assets.library_ref("prev_page"),
        bg_color="#26262c",
        action=Action("folder_back", {}),
    )


def _folder(name: str, icon: str, entries: list[tuple[int, KeyConfig]],
            *, bg: str = "#152035") -> KeyConfig:
    """Build an open_folder key whose first page holds `entries` + a Back key."""
    page = Page(name="Main")
    for idx, kc in entries:
        page.keys[idx] = kc
    last = int(DEVICE_PROFILE.get("key_count", 15) or 15)
    page.keys[last] = _back_key()
    return KeyConfig(
        label=name,
        icon=assets.library_ref(icon),
        bg_color=bg,
        action=Action("open_folder", {}),
        folder=Folder(name=name, pages=[page]),
    )


def apply_starter_layout(cfg: DeckConfig) -> None:
    """Fill the active profile's first page with Streaming / Recording / General.

    Safe to call on a fresh DeckConfig(); does not clear other profiles.
    """
    if not cfg.profiles:
        return
    page = cfg.profiles[0].pages[0] if cfg.profiles[0].pages else None
    if page is None:
        page = Page(name="Main")
        cfg.profiles[0].pages = [page]

    streaming = _folder("Streaming", "web", [
        (1, _key("Start", "play", Action("obs_streaming", {"cmd": "start"}),
                 bg="#0d3320")),
        (2, _key("Stop", "stop", Action("obs_streaming", {"cmd": "stop"}),
                 bg="#3a1515")),
        (3, _key("Toggle", "web", Action("obs_streaming", {"cmd": "toggle"}))),
    ], bg="#0d2840")

    recording = _folder("Recording", "stop", [
        (1, _key("Start", "play", Action("obs_recording", {"cmd": "start"}),
                 bg="#0d3320")),
        (2, _key("Stop", "stop", Action("obs_recording", {"cmd": "stop"}),
                 bg="#3a1515")),
        (3, _key("Toggle", "camera",
                 Action("obs_recording", {"cmd": "toggle"}))),
    ], bg="#401010")

    # Stream+Record and Stop All as multi-actions.
    stream_and_record = Action("multi", {"steps": [
        {"action": Action("obs_streaming", {"cmd": "start"}).to_dict(),
         "delay": 0.2},
        {"action": Action("obs_recording", {"cmd": "start"}).to_dict(),
         "delay": 0},
    ]})
    stop_all = Action("multi", {"steps": [
        {"action": Action("obs_streaming", {"cmd": "stop"}).to_dict(),
         "delay": 0.1},
        {"action": Action("obs_recording", {"cmd": "stop"}).to_dict(),
         "delay": 0},
    ]})

    general = _folder("General", "folder", [
        (1, _key("Mic Mute", "mute",
                 Action("obs_mute", {"input": "Mic/Aux", "state": "toggle"}))),
        (2, _key("Stream", "web",
                 Action("obs_streaming", {"cmd": "toggle"}))),
        (3, _key("Record", "camera",
                 Action("obs_recording", {"cmd": "toggle"}))),
        (4, _key("Stream+Rec", "star", stream_and_record, bg="#152035")),
        (5, _key("Stop All", "power", stop_all, bg="#3a1515")),
        (6, _key("Scene Live", "camera",
                 Action("obs_scene", {"scene": "Live"}))),
        (7, _key("Scene BRB", "dot",
                 Action("obs_scene", {"scene": "BRB"}))),
    ], bg="#203015")

    # Comedy soundboard — bleeps, farts, and deliberately unfitting jingles.
    # Overlapping presses mix (each play is a separate player process).
    sounds = _folder("Sounds", "play", [
        (1, _snd("Bleep", "bleep", icon="mute", bg="#203040")),
        (2, _snd("Airhorn", "airhorn", bg="#402010")),
        (3, _snd("Squeak", "fart_squeak", bg="#302018")),
        (4, _snd("Wet", "fart_wet", bg="#302018")),
        (5, _snd("Trumpet", "fart_trumpet", bg="#302018")),
        (6, _snd("Laugh", "laugh")),
        (7, _snd("Rimshot", "rimshot")),
        (8, _snd("Wah-wah", "sad_trombone")),
        (9, _snd("Boing", "boing")),
        (10, _snd("Slide", "slide_down")),
        (11, _snd("Circus", "circus", bg="#203018")),
        (12, _snd("Elevator", "elevator", bg="#203018")),
        (13, _snd("Kazoo", "kazoo", bg="#203018")),
        (14, _snd("Random!", "random_funny", icon="star", bg="#3a2040")),
    ], bg="#2a1840")

    page.keys[1] = streaming
    page.keys[2] = recording
    page.keys[3] = general
    page.keys[4] = sounds
    page.keys[5] = _snd("Fart?", "random_fart", icon="star", bg="#302018")
    page.keys[6] = _snd("Jingle?", "random_music", icon="play", bg="#203018")
