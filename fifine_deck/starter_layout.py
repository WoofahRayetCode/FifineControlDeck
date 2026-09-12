"""Starter layout for a brand-new configuration.

Applied only when DeckConfig.load() creates a fresh config (missing or
unreadable file). Existing user configs are never rewritten.

Includes OBS control folders plus a multi-page Memes folder of MyInstants
sound bites.
"""
from __future__ import annotations

from . import assets
from .device import DEVICE_PROFILE
from .model import Action, DeckConfig, Folder, KeyConfig, Page

# Short key labels for the Memes folder (clip id, label).
_MEME_CLIPS: list[tuple[str, str]] = [
    ("vine_boom", "Boom"),
    ("wtf_boom", "WTF!"),
    ("wrong_answer_buzzer", "Wrong"),
    ("win_xp_error", "XP Err"),
    ("taco_bell_bong", "Taco"),
    ("bruh", "Bruh"),
    ("emotional_damage", "Emotional"),
    ("shocked_sound", "Shocked"),
    ("asian_meme_huh", "Asian Huh"),
    ("french_meme_song", "French"),
    ("du_bist_gut_genug", "Du bist"),
    ("directed_by_robert_weide", "Weide"),
    ("okay_lets_go", "Let's Go"),
    ("rat_dance_music", "Rat Dance"),
    ("metal_gear_alert", "MGS Alert"),
    ("gas_gas_gas", "Gas Gas"),
    ("wet_fart", "Wet Fart"),
    ("gegagedigedagedago", "Gegaged"),
    ("sad_violin", "Sad Violin"),
    ("e_meme", "E"),
    ("deja_vu_fade", "Déjà Vu"),
    ("samsung_notification", "Samsung"),
    ("hello_meme", "Hello"),
    ("ah_shit_here_we_go_again", "Here We Go"),
    ("shooting_stars", "Shooting"),
    ("doom_music", "Doom"),
    ("oblivion_npc_theme", "Oblivion"),
    ("meme_67", "67"),
    ("butter_dog", "Butter Dog"),
    ("arabic_nokia", "Arabic Nokia"),
    ("rizz", "Rizz"),
    ("roblox_oof", "Oof"),
    ("multi_yeet", "Yeet"),
    ("gawd_dayum", "Dayum"),
    ("oh_my_god", "OMG"),
    ("huh_cat", "Huh?"),
    ("goofy_slip", "Slip"),
    ("spongebob_fail", "SB Fail"),
    ("gta_wasted", "Wasted"),
    ("tf2_scout_scream", "Scout"),
    ("faaah", "FAAAH"),
    ("fast_clapping", "Clap"),
    ("zelda_hey_listen", "Listen"),
    ("bark_fart", "Bark"),
    ("brain_fart", "Brain"),
    ("dexter_theme", "Dexter"),
    ("mii_channel_music", "Mii"),
    ("tiktok_india", "TikTok"),
    ("zelda_item_get", "Item!"),
    ("sonic_rings_falling", "Rings"),
]


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


# Fixed slots for page navigation on a 15-key deck.
# Root pages: Prev on 13, Next on 15 (bottom-right). Slot 14 stays free.
# Folder pages: Next on 14, Back on 15 (Prev is never used — Back covers it).
PAGE_NAV_PREV_SLOT = 13
PAGE_NAV_NEXT_SLOT_FOLDER = 14


def _page_nav_next_slot(*, in_folder: bool) -> int:
    if in_folder:
        return PAGE_NAV_NEXT_SLOT_FOLDER
    return int(DEVICE_PROFILE.get("key_count", 15) or 15)


def _page_nav_key(kind: str) -> KeyConfig:
    if kind == "prev":
        return _key("Prev", "prev_page", Action("prev_page", {}), bg="#203040")
    return _key("Next", "next_page", Action("next_page", {}), bg="#203040")


def _is_page_nav_slot(kc: KeyConfig | None, kind: str) -> bool:
    """True if the slot is empty or already the matching Prev/Next action."""
    if kc is None or kc.is_empty():
        return True
    want = "prev_page" if kind == "prev" else "next_page"
    return kc.action.type == want


def apply_page_nav_keys(pages: list[Page], *, in_folder: bool = False) -> None:
    """Place page-nav chips by position.

    Profile (root) pages: first gets Next (bottom-right), last gets Prev,
    middle get both. Folder pages: only Next on non-last pages (slot 14) —
    never Prev. The folder's Back key is smart (previous page, or exit on
    page 1), so a separate Prev would double up with Back. Single-page
    boards get neither.

    Only empty slots or slots that already hold the matching nav action are
    written; other user keys on those slots are left alone.
    """
    n = len(pages)
    next_slot = _page_nav_next_slot(in_folder=in_folder)
    for i, page in enumerate(pages):
        if in_folder:
            want_prev = False
            want_next = n > 1 and i < n - 1
        else:
            want_prev = n > 1 and i > 0
            want_next = n > 1 and i < n - 1
        _apply_nav_slot(page, PAGE_NAV_PREV_SLOT, "prev", want_prev)
        _apply_nav_slot(page, next_slot, "next", want_next)
        # Drop a Next left on the other nav column after we moved the
        # canonical slot (root 14→15, or an old stray).
        _clear_stray_next(page, next_slot, want_next=want_next)


def _clear_stray_next(page: Page, next_slot: int, *, want_next: bool) -> None:
    last = int(DEVICE_PROFILE.get("key_count", 15) or 15)
    for slot in (PAGE_NAV_NEXT_SLOT_FOLDER, last):
        if slot == next_slot:
            continue
        kc = page.keys.get(slot)
        if kc is None or kc.is_empty() or kc.folder is not None:
            continue
        if kc.action.type != "next_page":
            continue
        # Clear when we don't want Next, or when the canonical slot already
        # holds Next (duplicate).
        can = page.keys.get(next_slot)
        if not want_next or (can is not None and can.action.type == "next_page"):
            page.keys[slot] = KeyConfig()


def _apply_nav_slot(page: Page, slot: int, kind: str, want: bool) -> None:
    existing = page.keys.get(slot)
    if want:
        if not _is_page_nav_slot(existing, kind):
            return
        page.keys[slot] = _page_nav_key(kind)
        return
    # Remove a leftover auto-nav chip when this side shouldn't exist.
    if existing is not None and _is_page_nav_slot(existing, kind) \
            and not existing.is_empty():
        page.keys[slot] = KeyConfig()


def apply_page_nav_everywhere(cfg: DeckConfig) -> None:
    """Ensure page-nav chips on every multi-page profile and nested folder."""
    for profile in cfg.profiles:
        apply_page_nav_keys(profile.pages)
        for page in profile.pages:
            _apply_page_nav_in_page_tree(page)


def _apply_page_nav_in_page_tree(page: Page) -> None:
    for kc in page.keys.values():
        if kc.folder is None:
            continue
        apply_page_nav_keys(kc.folder.pages, in_folder=True)
        for nested in kc.folder.pages:
            _apply_page_nav_in_page_tree(nested)


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


def build_soundboard_folder(
        clips: list[tuple[str, str]] | None = None,
        *,
        name: str = "Memes",
) -> KeyConfig:
    """Multi-page folder of play-sound keys.

    ``clips`` is [(clip_id, short_label), ...]. When omitted, every bundled
    clip from assets/sounds is included (Soundboard “All sounds folder” chip).
    Layout per page: keys 1–12 clips, 13/14 Prev/Next by position, 15 Back.
    """
    if clips is None:
        from . import sounds
        clips = []
        for c in sounds.list_clips():
            if not c.get("path"):
                continue
            label = (c.get("label") or c["name"]).strip()
            if " (" in label:
                label = label.split(" (", 1)[0].strip() or c["name"]
            clips.append((c["name"], label))
    last = int(DEVICE_PROFILE.get("key_count", 15) or 15)
    per_page = 12
    pages: list[Page] = []
    for page_i in range(0, len(clips), per_page):
        chunk = clips[page_i:page_i + per_page]
        page = Page(name=f"{name} {page_i // per_page + 1}")
        for slot, (clip, label) in enumerate(chunk, start=1):
            page.keys[slot] = _snd(label, clip, bg="#402028")
        page.keys[last] = _back_key()
        pages.append(page)
    if not pages:
        pages = [Page(name=f"{name} 1")]
        pages[0].keys[last] = _back_key()
    apply_page_nav_keys(pages, in_folder=True)
    return KeyConfig(
        label=name,
        icon=assets.library_ref("star"),
        bg_color="#3a1840",
        action=Action("open_folder", {}),
        folder=Folder(name=name, pages=pages),
    )


def _memes_folder() -> KeyConfig:
    """Starter Memes folder (curated clip order and short labels)."""
    return build_soundboard_folder(list(_MEME_CLIPS), name="Memes")


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
        (1, _key("Go Live", "play", Action("obs_streaming", {"cmd": "start"}),
                 bg="#0d3320")),
        (2, _key("End Live", "stop", Action("obs_streaming", {"cmd": "stop"}),
                 bg="#3a1515")),
        (3, _key("Toggle", "web", Action("obs_streaming", {"cmd": "toggle"}))),
    ], bg="#0d2840")

    recording = _folder("Recording", "stop", [
        (1, _key("Record", "play", Action("obs_recording", {"cmd": "start"}),
                 bg="#0d3320")),
        (2, _key("Stop Rec", "stop", Action("obs_recording", {"cmd": "stop"}),
                 bg="#3a1515")),
        (3, _key("Toggle", "camera",
                 Action("obs_recording", {"cmd": "toggle"}))),
    ], bg="#401010")

    scenes = _folder("Scenes", "camera", [
        (1, _key("Soon", "dot",
                 Action("obs_scene", {"scene": "Starting Soon"}),
                 bg="#2a2035")),
        (2, _key("BRB", "dot",
                 Action("obs_scene", {"scene": "BRB"}), bg="#352820")),
        (3, _key("Live", "camera",
                 Action("obs_scene", {"scene": "Live"}), bg="#0d2840")),
        (4, _key("Game", "star",
                 Action("obs_scene", {"scene": "Game Capture"}),
                 bg="#152035")),
    ], bg="#1a2030")

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
        (2, _key("Go Live", "web",
                 Action("obs_streaming", {"cmd": "start"}), bg="#0d3320")),
        (3, _key("Record", "camera",
                 Action("obs_recording", {"cmd": "start"}))),
        (4, _key("Live+Rec", "star", stream_and_record, bg="#152035")),
        (5, _key("Stop All", "power", stop_all, bg="#3a1515")),
        (6, _key("Clip", "camera",
                 Action("chatterino",
                        {"command": "/clip", "window": "chatterino"}),
                 bg="#3a1840")),
    ], bg="#203015")

    memes = _memes_folder()

    page.keys[1] = streaming
    page.keys[2] = recording
    page.keys[3] = scenes
    page.keys[4] = general
    page.keys[5] = memes
    page.keys[6] = _snd("Random!", "random_funny", icon="star", bg="#3a2040")
    page.keys[7] = _snd("Fart?", "random_fart", icon="star", bg="#302018")
    page.keys[8] = _snd("Jingle?", "random_music", icon="play", bg="#203018")
