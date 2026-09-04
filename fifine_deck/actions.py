"""
Action engine: executes the action bound to a key/knob gesture on Linux.

Actions that only affect the OS (launch, command, hotkey, media, volume, url,
text) are executed here. Actions that affect the deck itself (switch page /
profile, brightness) are delegated to an ActionContext supplied by the runtime
controller, because they need device + config state.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from typing import Optional, Protocol

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment detection (done once).
# ---------------------------------------------------------------------------
IS_WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY")) or \
    os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"

# Confined snap: USB access needs the raw-usb / hardware-observe interfaces,
# which are manual-connect by default (a snap cannot connect them to itself),
# so the device is inert until the user runs `snap connect`.
IN_SNAP = bool(os.environ.get("SNAP") and os.environ.get("SNAP_NAME"))


def _snap_is_classic() -> bool:
    """True if this snap was built with classic confinement (reads meta/snap.yaml)."""
    snap = os.environ.get("SNAP")
    if not snap:
        return False
    try:
        with open(os.path.join(snap, "meta", "snap.yaml"), encoding="utf-8") as f:
            return any(line.strip() == "confinement: classic" for line in f)
    except OSError:
        return False


# A classic snap CAN open /dev/hidraw directly, but only if the host has the
# udev rule (a snap cannot install one) — so its guidance differs from strict.
IN_SNAP_CLASSIC = IN_SNAP and _snap_is_classic()


def _has(cmd: str) -> bool:
    """Is `cmd` available on PATH?"""
    return shutil.which(cmd) is not None


def _audio_backend() -> str:
    if _has("wpctl"):
        return "pipewire"
    if _has("pactl"):
        return "pulseaudio"
    return ""


AUDIO = _audio_backend()

# Ordered preference of a keystroke-injection tool for the current session.
if IS_WAYLAND:
    _KEY_TOOLS = ["ydotool", "wtype", "xdotool"]
else:
    _KEY_TOOLS = ["xdotool", "ydotool", "wtype"]
KEY_TOOL = next((t for t in _KEY_TOOLS if _has(t)), "")
HAS_PLAYERCTL = _has("playerctl")


class ActionContext(Protocol):
    """Deck-side operations an action may request from the runtime controller."""
    def switch_profile(self, profile_id: str) -> None: ...
    def next_profile(self) -> None: ...
    def prev_profile(self) -> None: ...
    def goto_page(self, index: int) -> None: ...
    def next_page(self) -> None: ...
    def prev_page(self) -> None: ...
    def set_brightness(self, percent: int) -> None: ...
    def adjust_brightness(self, delta: int) -> None: ...
    def sleep_screen(self) -> None: ...
    def obs_connection(self) -> tuple[str, int, str]: ...
    def sound_output(self) -> tuple[str, bool]: ...


# ---------------------------------------------------------------------------
# Action metadata (drives the GUI editor). Each entry: label + param spec.
# param spec: list of (key, kind, label) where kind in text/multiline/choice.
# ---------------------------------------------------------------------------
ACTION_TYPES: dict[str, dict] = {
    "none":          {"label": "— None —", "params": []},
    "launch_app":    {"label": "Launch application", "params": [("command", "text", "Command / app")]},
    "run_command":   {"label": "Run shell command", "params": [("command", "multiline", "Shell command")]},
    "open_url":      {"label": "Open website / file", "params": [("url", "text", "URL or path")]},
    "hotkey":        {"label": "Send hotkey", "params": [("keys", "text", "e.g. ctrl+shift+m")]},
    "text":          {"label": "Type text", "params": [
        ("text", "multiline", "Text to type"),
        ("press_enter", "choice:no,yes", "Press Enter after typing (send message)")]},
    "password":      {"label": "Type password", "params": [("password", "password", "Password")]},
    "media":         {"label": "Media control", "params": [("cmd", "choice:play-pause,next,previous,stop", "Command")]},
    "volume":        {"label": "Volume", "params": [("cmd", "choice:up,down,mute", "Command"), ("step", "text", "Step % (up/down)")]},
    "play_sound":    {"label": "Play sound", "params": [
        ("clip", "sound", "Clip"),
        ("file", "filepath", "Custom audio file"),
        ("volume", "text", "Volume % (1–100)")]},
    "close_app":     {"label": "Close application", "params": [("target", "text", "App / window name")]},
    "chatterino":    {"label": "Chatterino command", "params": [
        ("command", "text", "Command (default /clip)"),
        ("window", "text", "Window class / title (default chatterino)")]},
    "next_page":     {"label": "Next page", "params": []},
    "prev_page":     {"label": "Previous page", "params": []},
    "goto_page":     {"label": "Go to page #", "params": [("page", "text", "Page number (1-based)")]},
    "switch_profile": {"label": "Switch profile", "params": [("profile_id", "profiles", "Profile")]},
    "next_profile":  {"label": "Next profile (Scene Shift)", "params": []},
    "prev_profile":  {"label": "Previous profile", "params": []},
    "brightness":    {"label": "Brightness", "params": [("mode", "choice:set,up,down", "Mode"), ("value", "text", "Value / step")]},
    "sleep_screen":  {"label": "Sleep screen", "params": []},
    "monitor":       {"label": "System monitor", "params": [
        ("metric", "choice:cpu,ram,vram,gpu,gputemp,cputemp,igpu,igpuvram,igpupower,igputemp,temp,net,disk,clock,procram,cpupower,gpupower,twitchviewers,twitchuptime", "Metric"),
        ("style", "choice:number,gauge,graph", "Style"),
        ("interval", "text", "Refresh every (seconds)"),
        ("target", "text", "Disk / iface / temp / process / Twitch login"),
        ("clock_format", "choice:auto,24h,24h+seconds,12h,12h+seconds", "Clock format"),
        ("clock_date", "choice:auto,iso,us,none", "Clock date"),
    ]},
    "open_folder":   {"label": "Open folder", "params": []},
    "folder_back":   {"label": "Back (prev page / exit folder)", "params": []},
    "multi":         {"label": "Multi-action (steps)", "params": []},  # edited specially
    # OBS Studio via obs-websocket v5 (Options → OBS settings for host/port/password).
    "obs_scene":     {"label": "OBS: Switch scene", "params": [
        ("scene", "text", "Scene name")]},
    "obs_preview_scene": {"label": "OBS: Preview scene", "params": [
        ("scene", "text", "Scene name")]},
    "obs_transition": {"label": "OBS: Studio transition", "params": []},
    "obs_source":    {"label": "OBS: Show / hide source", "params": [
        ("scene", "text", "Scene name"),
        ("source", "text", "Source name"),
        ("state", "choice:show,hide,toggle", "State")]},
    "obs_recording": {"label": "OBS: Recording", "params": [
        ("cmd", "choice:start,stop,toggle", "Command")]},
    "obs_streaming": {"label": "OBS: Streaming", "params": [
        ("cmd", "choice:start,stop,toggle", "Command")]},
    "obs_mute":      {"label": "OBS: Mute input", "params": [
        ("input", "text", "Input / source name"),
        ("state", "choice:mute,unmute,toggle", "State")]},
}


# Catalog grouping for the drag-and-drop sidebar: (category, [entries]).
# Each entry is either an action-type string, or a preset dict:
#   {"type": "monitor", "label": "GPU usage", "params": {"metric": "gpu"}}
# Presets drop the same action type with params pre-filled (System / Soundboard).
# Soundboard chips are built from assets/sounds/index.json — see
# get_action_catalog().
_ACTION_CATALOG_CORE = [
    ("Application", ["launch_app", "run_command", "open_url", "close_app"]),
    ("Keyboard",    ["hotkey", "text", "password"]),
    ("Media",       ["media", "volume", "play_sound"]),
    ("OBS", [
        "obs_scene", "obs_preview_scene", "obs_transition",
        "obs_source", "obs_recording", "obs_streaming", "obs_mute",
        {"type": "obs_streaming", "label": "Go Live",
         "params": {"cmd": "start"}},
        {"type": "obs_streaming", "label": "End Stream",
         "params": {"cmd": "stop"}},
        {"type": "obs_recording", "label": "Record",
         "params": {"cmd": "start"}},
        {"type": "obs_recording", "label": "Stop Record",
         "params": {"cmd": "stop"}},
        {"type": "obs_scene", "label": "Starting Soon",
         "params": {"scene": "Starting Soon"}},
        {"type": "obs_scene", "label": "BRB",
         "params": {"scene": "BRB"}},
        {"type": "obs_scene", "label": "Live",
         "params": {"scene": "Live"}},
        {"type": "obs_scene", "label": "Game Capture",
         "params": {"scene": "Game Capture"}},
        {"type": "obs_mute", "label": "Mic Mute",
         "params": {"input": "Mic/Aux", "state": "toggle"}},
    ]),
    ("Twitch", [
        {"type": "chatterino", "label": "Create clip",
         "params": {"command": "/clip", "window": "chatterino"}},
        "chatterino",
    ]),
    ("System", [
        "monitor",
        {"type": "monitor", "label": "GPU usage",
         "params": {"metric": "gpu"}},
        {"type": "monitor", "label": "GPU VRAM",
         "params": {"metric": "vram"}},
        {"type": "monitor", "label": "GPU wattage",
         "params": {"metric": "gpupower"}},
        {"type": "monitor", "label": "GPU temp",
         "params": {"metric": "gputemp"}},
        {"type": "monitor", "label": "iGPU usage",
         "params": {"metric": "igpu"}},
        {"type": "monitor", "label": "iGPU VRAM",
         "params": {"metric": "igpuvram"}},
        {"type": "monitor", "label": "iGPU wattage",
         "params": {"metric": "igpupower"}},
        {"type": "monitor", "label": "iGPU temp",
         "params": {"metric": "igputemp"}},
        {"type": "monitor", "label": "CPU wattage",
         "params": {"metric": "cpupower"}},
        {"type": "monitor", "label": "CPU temp",
         "params": {"metric": "cputemp"}},
        {"type": "monitor", "label": "RAM usage",
         "params": {"metric": "ram"}},
        {"type": "monitor", "label": "Game process RAM",
         "params": {"metric": "procram"}},
    ]),
    ("Deck",        ["next_page", "prev_page", "goto_page", "switch_profile",
                     "next_profile", "prev_profile", "brightness", "sleep_screen"]),
    ("Folders",     ["open_folder", "folder_back"]),
    ("Advanced",    ["multi"]),
]


def _short_sound_label(label: str, fallback: str = "Sound") -> str:
    """Sidebar / key label: drop the trailing ' (MyInstants)' source tag."""
    text = (label or "").strip() or fallback
    if " (" in text:
        text = text.split(" (", 1)[0].strip() or fallback
    return text


def soundboard_catalog_entries() -> list:
    """Play-sound presets for every bundled meme clip + random picks.

    Leading chip drops a multi-page folder of every clip (see
    ``preset=soundboard`` handling in the main window).
    """
    from . import sounds
    entries: list = [
        {"type": "open_folder", "label": "All sounds folder",
         "params": {"preset": "soundboard"}},
        {"type": "play_sound", "label": "Random Funny",
         "params": {"clip": "random_funny"}},
        {"type": "play_sound", "label": "Random Fart",
         "params": {"clip": "random_fart"}},
        {"type": "play_sound", "label": "Random Music",
         "params": {"clip": "random_music"}},
        {"type": "play_sound", "label": "Random Any",
         "params": {"clip": "random"}},
    ]
    for clip in sounds.list_clips():
        if not clip.get("path"):
            continue
        name = clip["name"]
        entries.append({
            "type": "play_sound",
            "label": _short_sound_label(clip.get("label") or name, name),
            "params": {"clip": name},
        })
    return entries


def get_action_catalog() -> list:
    """Full sidebar catalog, with Soundboard chips after Media."""
    out: list = []
    for cat, entries in _ACTION_CATALOG_CORE:
        out.append((cat, entries))
        if cat == "Media":
            out.append(("Soundboard", soundboard_catalog_entries()))
    return out


# Snapshot for imports/tests; GUI rebuilds via get_action_catalog().
ACTION_CATALOG = get_action_catalog()


def catalog_entry_type(entry) -> str:
    """Action type for a catalog entry (string or preset dict)."""
    if isinstance(entry, str):
        return entry
    return str(entry.get("type", "") or "")


def catalog_entry_label(entry) -> str:
    """Sidebar label for a catalog entry."""
    if isinstance(entry, str):
        return ACTION_TYPES.get(entry, {}).get("label", entry)
    label = entry.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    t = catalog_entry_type(entry)
    return ACTION_TYPES.get(t, {}).get("label", t)


def catalog_entry_params(entry) -> dict:
    """Default params applied when the entry is dropped onto a key."""
    if isinstance(entry, str):
        return {}
    params = entry.get("params") or {}
    return dict(params) if isinstance(params, dict) else {}


def encode_catalog_drag(entry) -> str:
    """Encode a catalog entry for MIME (no ':' — page id uses that separator).

    Plain types stay bare (`volume`). Presets become `type?k=v&k2=v2`
    (`monitor?metric=gpu`).
    """
    from urllib.parse import urlencode
    t = catalog_entry_type(entry)
    params = catalog_entry_params(entry)
    if not params:
        return t
    # Stable order so tests and round-trips are predictable.
    q = urlencode(sorted((str(k), str(v)) for k, v in params.items()))
    return f"{t}?{q}"


def parse_catalog_drag(token: str) -> tuple[str, dict]:
    """Inverse of encode_catalog_drag → (action_type, params)."""
    from urllib.parse import parse_qsl
    token = (token or "").strip()
    if not token:
        return ("none", {})
    atype, sep, query = token.partition("?")
    atype = atype.strip() or "none"
    if not sep:
        return (atype, {})
    params = {k: v for k, v in parse_qsl(query, keep_blank_values=True)}
    return (atype, params)

# A default library-icon name + label to auto-assign when an action is dropped.
ACTION_DEFAULT_ICON = {
    "launch_app": ("home", "App"),
    "run_command": ("terminal", "Run"),
    "open_url": ("web", "Web"),
    "hotkey": ("dot", "Hotkey"),
    "text": ("dot", "Text"),
    "password": ("lock", "Password"),
    "media": ("play", "Play"),
    "volume": ("volume_up", "Volume"),
    "play_sound": ("play", "Sound"),
    "close_app": ("power", "Close"),
    "chatterino": ("camera", "Clip"),
    "next_page": ("next_page", "Next"),
    "prev_page": ("prev_page", "Prev"),
    "goto_page": ("next_page", "Page"),
    "switch_profile": ("settings", "Profile"),
    "next_profile": ("next_page", "Scene ▶"),
    "prev_profile": ("prev_page", "Scene ◀"),
    "brightness": ("brightness_up", "Bright"),
    "sleep_screen": ("dot", "Sleep"),
    # monitor: no icon/label on purpose — the live readout IS the key face,
    # and a library icon would overpaint it between ticks.
    "monitor": ("", ""),
    "open_folder": ("folder", "Folder"),
    "folder_back": ("prev_page", "Back"),
    "multi": ("star", "Multi"),
    "obs_scene": ("camera", "Scene"),
    "obs_preview_scene": ("camera", "Preview"),
    "obs_transition": ("next_page", "Cut"),
    "obs_source": ("dot", "Source"),
    "obs_recording": ("stop", "Record"),
    "obs_streaming": ("web", "Stream"),
    "obs_mute": ("mute", "Mute"),
}


def default_icon_for(action) -> tuple[str, str]:
    """Best (library-icon-name, label) for an action, following its sub-command
    so e.g. Volume up/down/mute each get their own icon."""
    t = action.type
    p = action.params
    # Params values are copied verbatim from JSON, so a list or dict here is
    # unhashable and dict.get(raw_value) raises TypeError — which escaped the
    # drag-and-drop slot, so dropping an action onto such a key silently did
    # nothing at all. Coerce to the string form the lookups expect.
    def _pick(key: str, default: str) -> str:
        v = p.get(key, default)
        return v if isinstance(v, str) else default
    if t == "volume":
        return ({"up": "volume_up", "down": "volume_down", "mute": "mute"}
                .get(_pick("cmd", "up"), "volume_up"), "Volume")
    if t == "media":
        return ({"play-pause": "play", "next": "next", "previous": "prev",
                 "stop": "stop"}.get(_pick("cmd", "play-pause"), "play"), "Media")
    if t == "play_sound":
        clip = _pick("clip", "bruh")
        # Prefer the bundled index label (short), then known randoms / custom.
        from . import sounds
        for c in sounds.list_clips():
            if c["name"] == clip:
                return ("play", _short_sound_label(c.get("label") or clip, clip))
        labels = {
            "random_fart": "Fart?", "random_funny": "Funny?",
            "random_music": "Jingle?", "random": "Random",
            "custom": "Sound",
        }
        return ("play", labels.get(clip, _short_sound_label(clip, "Sound")))
    if t == "brightness":
        return ({"up": "brightness_up", "down": "brightness_down",
                 "set": "brightness_up"}.get(_pick("mode", "set"), "brightness_up"), "Bright")
    if t == "obs_recording":
        return ({"start": ("play", "Record"), "stop": ("stop", "Stop Rec"),
                 "toggle": ("stop", "Record")}
                .get(_pick("cmd", "toggle"), ("stop", "Record")))
    if t == "obs_streaming":
        return ({"start": ("web", "Go Live"), "stop": ("stop", "End Live"),
                 "toggle": ("web", "Stream")}
                .get(_pick("cmd", "toggle"), ("web", "Stream")))
    if t == "obs_mute":
        return ({"mute": "mute", "unmute": "mic", "toggle": "mute"}
                .get(_pick("state", "toggle"), "mute"), "Mic Mute")
    if t == "obs_scene":
        scene = _pick("scene", "")
        key = scene.strip().lower()
        scene_faces = {
            "starting soon": ("dot", "Soon"),
            "brb": ("dot", "BRB"),
            "be right back": ("dot", "BRB"),
            "live": ("camera", "Live"),
            "main": ("camera", "Live"),
            "game capture": ("star", "Game"),
            "game": ("star", "Game"),
        }
        if key in scene_faces:
            return scene_faces[key]
        short = scene.strip()[:10] if scene.strip() else "Scene"
        return ("camera", short)
    if t == "obs_source":
        return ({"show": "dot", "hide": "dot", "toggle": "dot"}
                .get(_pick("state", "toggle"), "dot"), "Source")
    if t == "chatterino":
        cmd = _pick("command", "/clip").strip() or "/clip"
        if cmd.lstrip("/").lower() == "clip":
            return ("camera", "Clip")
        short = cmd if len(cmd) <= 10 else cmd[:9] + "…"
        return ("camera", short)
    return ACTION_DEFAULT_ICON.get(t, ("", ""))


# Variables a bundled launcher sets so OUR interpreter and OUR Qt resolve
# inside the bundle. Every one of them is poison for anything else we exec,
# and everything we exec is a host program: the user's apps, and the helpers
# (wpctl, playerctl, xdotool, xdg-open, pkexec).
#
# The AppImage's AppRun exports PYTHONHOME, QT_PLUGIN_PATH and LD_LIBRARY_PATH;
# the classic-snap wrapper exports those plus PYTHONPATH and
# QT_QPA_PLATFORM_PLUGIN_PATH. Inherited by a child, PYTHONHOME sends a host
# python3 looking for its stdlib in our 3.12 tree, where it dies with
# "No module named 'encodings'" before running a line — the exact failure the
# snap wrapper's own comment describes for its own interpreter. LD_LIBRARY_PATH
# puts our bundled libQt6 ahead of a host Qt app's own.
#
# So a key bound to meld, virt-manager, solaar or any python3 script simply did
# not start, and only on the AppImage and snap builds.
_BUNDLE_ENV_VARS = (
    "PYTHONHOME", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
    "LD_LIBRARY_PATH", "LD_PRELOAD",
    "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
)


def child_env() -> dict:
    """The environment for a program we exec on the user's behalf.

    Our launchers stash whatever the host had in FIFINE_HOST_<VAR> before
    overwriting it, so the honest fix is to put those values back. When there
    is no stash and we are inside OUR bundle, the variable is ours alone and is
    dropped entirely.

    "Our bundle" is signalled by FIFINE_IN_BUNDLE=1, which only our AppRun and
    snap launcher set — NOT by APPDIR or SNAP. Those are too generic: APPDIR is
    exported by any AppImage and by assorted build tooling, and SNAP leaks into
    anything spawned from inside a snap, so keying on them meant a plain
    .deb/PPA/source app whose environment happened to carry one would strip
    PYTHONPATH/LD_LIBRARY_PATH from every program a key launched — the opposite
    of the passthrough this promises. Older bundles (0.12.0/0.12.1) predate the
    marker but DO write the FIFINE_HOST_* stashes, so their real bundle vars are
    still restored; only a bundle var the host never set is missed there, which
    is the same "drop it" outcome by a longer road.

    Outside our bundle nothing matches and the environment passes through
    untouched, so .deb, PPA and source installs are unaffected.
    """
    env = dict(os.environ)
    in_bundle = env.get("FIFINE_IN_BUNDLE") == "1"
    env.pop("FIFINE_IN_BUNDLE", None)      # never hand this marker to a child
    for var in _BUNDLE_ENV_VARS:
        saved = env.pop("FIFINE_HOST_" + var, None)
        if saved is not None:
            env[var] = saved
        elif in_bundle:
            env.pop(var, None)
    return env


def _popen_detached(args, shell=False):
    """Launch a detached process (survives the app exiting)."""
    subprocess.Popen(
        args, shell=shell, start_new_session=True, env=child_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )


def _run(args, input_text: bytes | None = None, **kw):
    """subprocess.run with a timeout + error guard so a hung helper (wpctl,
    playerctl, xdotool, …) can never freeze the action worker thread.

    `input_text` is written to the child's stdin. Anything secret MUST travel
    this way and never in `args`: /proc/<pid>/cmdline is world-readable, so a
    password in argv is readable by every process on the machine (and by any
    `ps`/monitoring sample) for the lifetime of the helper. The failure log
    below prints the exception, which carries argv — another reason the secret
    must not be there.
    """
    kw.setdefault("timeout", 8)
    kw.setdefault("stderr", subprocess.DEVNULL)
    # Helpers are host binaries too, so they get the same de-bundled
    # environment as a user-launched app — see child_env().
    kw.setdefault("env", child_env())
    try:
        subprocess.run(args, input=input_text, **kw)
    except Exception as e:
        log.warning("command failed: %s", e)


# Linux input-event key codes for translating hotkey names -> ydotool keycodes.
_KEYCODES = {
    "ctrl": 29, "control": 29, "ctrl_r": 97, "shift": 42, "shift_r": 54,
    "alt": 56, "alt_r": 100, "altgr": 100, "super": 125, "meta": 125,
    "win": 125, "logo": 125,
    "esc": 1, "escape": 1, "tab": 15, "enter": 28, "return": 28, "space": 57,
    "backspace": 14, "delete": 111, "del": 111, "insert": 110, "ins": 110,
    "home": 102, "end": 107, "pageup": 104, "pgup": 104, "pagedown": 109,
    "pgdn": 109, "up": 103, "down": 108, "left": 105, "right": 106,
    "minus": 12, "-": 12, "equal": 13, "=": 13, "comma": 51, ",": 51,
    "dot": 52, "period": 52, ".": 52, "slash": 53, "/": 53,
    "semicolon": 39, ";": 39, "capslock": 58, "printscreen": 99, "print": 99,
}
for _i, _c in enumerate("1234567890"):
    _KEYCODES[_c] = 2 + _i
for _c, _v in {"a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34,
               "h": 35, "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49,
               "o": 24, "p": 25, "q": 16, "r": 19, "s": 31, "t": 20, "u": 22,
               "v": 47, "w": 17, "x": 45, "y": 21, "z": 44}.items():
    _KEYCODES[_c] = _v
for _n in range(1, 11):
    _KEYCODES[f"f{_n}"] = 58 + _n            # F1=59 .. F10=68
_KEYCODES["f11"] = 87
_KEYCODES["f12"] = 88
for _n in range(13, 25):
    _KEYCODES[f"f{_n}"] = 183 + (_n - 13)    # F13=183 .. F24=194
# Common punctuation keys that previously worked on xdotool (X11) but were a
# silent no-op on ydotool because they were missing here.
_KEYCODES.update({
    "grave": 41, "`": 41, "bracketleft": 26, "[": 26, "bracketright": 27,
    "]": 27, "backslash": 43, "\\": 43, "apostrophe": 40, "'": 40,
})


def _ydotool_keycodes(combo: str):
    """Translate 'ctrl+shift+m' -> [(29),(42),(50)] input keycodes, or None."""
    codes = []
    for part in combo.split("+"):
        code = _KEYCODES.get(part.strip().lower())
        if code is None:
            return None
        codes.append(code)
    return codes


# The app's key vocabulary -> X keysym NAMES for xdotool / wtype. ydotool uses
# _KEYCODES (numeric) directly, but xdotool/wtype resolve keysym names, and the
# app's abbreviations and symbol forms are not valid keysyms (esc, del, pgup, and
# `-,.;/[]\'` etc.), so ctrl+esc and friends silently injected NOTHING on X11.
# Tokens not listed pass through unchanged: single letters/digits are valid
# keysyms, and ctrl/alt/shift/super are xdotool modifier aliases.
_X_KEYSYM = {
    "esc": "Escape", "escape": "Escape",
    "del": "Delete", "delete": "Delete",
    "ins": "Insert", "insert": "Insert",
    "tab": "Tab", "return": "Return", "space": "space",
    "ctrl_r": "Control_R", "shift_r": "Shift_R", "alt_r": "Alt_R",
    "pgup": "Prior", "pageup": "Prior", "pgdn": "Next", "pagedown": "Next",
    "enter": "Return", "backspace": "BackSpace", "capslock": "Caps_Lock",
    "printscreen": "Print", "print": "Print",
    "win": "super", "logo": "super", "meta": "super",
    "altgr": "ISO_Level3_Shift",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End",
    "-": "minus", ".": "period", "dot": "period", ",": "comma", "/": "slash",
    ";": "semicolon", "=": "equal", "`": "grave", "[": "bracketleft",
    "]": "bracketright", "\\": "backslash", "'": "apostrophe",
}


def _to_x_keysym(part: str) -> str:
    """Map one hotkey token to the X keysym name xdotool/wtype expect.

    X's name lookup is CASE-SENSITIVE: a lowercase "tab" or "f5" resolves to
    nothing and the backend drops the token without a word (its stderr is
    devnulled), pressing the remaining modifiers bare. So the app vocabulary
    is normalised here; function keys get their uppercase F programmatically.
    """
    p = part.strip()
    low = p.lower()
    if low in _X_KEYSYM:
        return _X_KEYSYM[low]
    if len(low) in (2, 3) and low[0] == "f" and low[1:].isdigit() \
            and 1 <= int(low[1:]) <= 24:
        return "F" + low[1:]
    return p


def _send_hotkey(combo: str) -> None:
    """Send a key combination like 'ctrl+shift+m'. Best-effort across tools."""
    combo = combo.strip()
    if not combo or not KEY_TOOL:
        if not KEY_TOOL:
            log.warning("no keystroke tool (install xdotool / ydotool / wtype)")
        return
    if KEY_TOOL == "xdotool":
        # Canonicalise each token to an X keysym so abbreviations (esc, pgup)
        # and symbol forms resolve instead of silently injecting nothing.
        combo_x = "+".join(_to_x_keysym(p) for p in combo.split("+"))
        _run(["xdotool", "key", "--clearmodifiers", combo_x],
                       stderr=subprocess.DEVNULL)
    elif KEY_TOOL == "wtype":
        parts = combo.split("+")
        # wtype's modifier vocabulary is shift/capslock/ctrl/logo/win/alt/altgr.
        # An unknown name makes it exit before emitting anything, so the WHOLE
        # combo is lost silently — map the app's right-hand modifier tokens
        # onto their plain form (wtype has no left/right distinction).
        _modmap = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt",
                   "shift": "shift", "super": "logo", "meta": "logo",
                   "win": "logo", "logo": "logo",
                   "ctrl_r": "ctrl", "shift_r": "shift", "alt_r": "alt",
                   "altgr": "altgr"}
        mods = [p.strip().lower() for p in parts[:-1]]
        key = _to_x_keysym(parts[-1])           # canonical keysym, keep its case
        # ...but the modifier ALIASES are xdotool spellings, not xkbcommon
        # keysym names, so a combo ending in a bare modifier ("alt+shift", the
        # layout toggle, or a lone "ctrl") made wtype exit before emitting
        # anything. Give them their real keysym names.
        key = {"super": "Super_L", "logo": "Super_L", "win": "Super_L",
               "meta": "Super_L", "ctrl": "Control_L", "control": "Control_L",
               "alt": "Alt_L", "shift": "Shift_L",
               "ctrl_r": "Control_R", "shift_r": "Shift_R",
               "alt_r": "Alt_R"}.get(key.lower(), key)
        args = ["wtype"]
        for m in mods:
            args += ["-M", _modmap.get(m, m)]
        args += ["-k", key]
        for m in mods:
            args += ["-m", _modmap.get(m, m)]
        _run(args, stderr=subprocess.DEVNULL)
    elif KEY_TOOL == "ydotool":
        # ydotool needs numeric keycodes: press all down (in order), release up.
        codes = _ydotool_keycodes(combo)
        if not codes:
            log.warning("hotkey '%s': unknown key name for ydotool", combo)
            return
        seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        _run(["ydotool", "key", *seq], stderr=subprocess.DEVNULL)


def _type_text(text: str) -> None:
    """Type `text` into the focused window.

    The text goes in on stdin, never argv. This is the same path the "type
    password" action takes, and argv is world-readable through
    /proc/<pid>/cmdline — putting the secret there would undo everything
    secret_store.py does to keep it off disk. All three helpers support it:
    xdotool via `--file -`, wtype via a bare `-`, ydotool via
    `--file /dev/stdin`.

    ydotool gets /dev/stdin rather than "-": 1.0.x treats "-" as stdin, but
    legacy 0.1.8 (jammy, still a supported .deb target) fopen()s a literal
    file named "-" and silently types nothing. /dev/stdin works with every
    implementation that opens the argument as a path.

    Reading from stdin also disables ydotool's escape handling, so text is
    typed literally (`\\n` stays two characters); a real newline still presses
    Return, which is what the multi-line editor produces.
    """
    if not KEY_TOOL:
        # Say so, exactly as _send_hotkey does. Returning silently meant a
        # "Type text" or "Type password" key on a machine with none of these
        # installed did nothing at all, with no log line and nothing on screen —
        # indistinguishable from the key simply not being bound.
        log.warning("no keystroke tool (install xdotool / ydotool / wtype); "
                    "nothing was typed")
        return
    data = text.encode()
    # Typing is inherently slow — ydotool defaults to ~40 ms per character —
    # so _run's 8 s default killed a long snippet mid-way (~200 chars) and left
    # half a line in the user's document with only a log line. Scale the budget
    # to the payload; the editor deliberately offers a multiline text field.
    timeout = max(8, int(len(text) * 0.06) + 5)
    if KEY_TOOL == "xdotool":
        _run(["xdotool", "type", "--clearmodifiers", "--file", "-"],
             input_text=data, timeout=timeout)
    elif KEY_TOOL == "wtype":
        _run(["wtype", "-"], input_text=data, timeout=timeout)
    elif KEY_TOOL == "ydotool":
        # /dev/stdin, not "-": see the docstring — legacy ydotool 0.1.8
        # fopen()s a literal "-" and silently types nothing.
        _run(["ydotool", "type", "--file", "/dev/stdin"],
             input_text=data, timeout=timeout)


def _close_app(target: str) -> None:
    """Close an app by window title/class (wmctrl) or process name (pkill)."""
    target = target.strip()
    if not target:
        return
    if _has("wmctrl"):
        _run(["wmctrl", "-c", target], stderr=subprocess.DEVNULL)
    elif _has("pkill"):
        # Match the process NAME, not the full command line: `pkill -f
        # <target>` substring-matches every process's argv, so a target like
        # "fifine" or "python" would kill the deck app itself and unrelated
        # processes. `-x` requires an exact comm match.
        _run(["pkill", "-x", target], stderr=subprocess.DEVNULL)
    else:
        log.warning("close needs 'wmctrl' or 'pkill'")


def _run_out(argv: list[str], timeout: float = 5.0) -> str:
    """Run a command and return stdout (empty string on failure)."""
    try:
        return subprocess.check_output(
            argv, stderr=subprocess.DEVNULL, timeout=timeout,
            text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _kwin_activate_class(wm_class: str) -> bool:
    """Activate a window by resourceClass via a short KWin script (Plasma Wayland)."""
    qdbus = "qdbus6" if _has("qdbus6") else ("qdbus" if _has("qdbus") else "")
    if not qdbus:
        return False
    # Escape for embedding in a JS string literal.
    cls = wm_class.replace("\\", "\\\\").replace('"', '\\"')
    # Plasma 5 used clientList/activeClient; Plasma 6 uses windowList/activeWindow.
    script = f"""
var cls = "{cls}".toLowerCase();
function matchWin(c) {{
    var rc = (c.resourceClass || "").toString().toLowerCase();
    var rn = (c.resourceName || "").toString().toLowerCase();
    var cap = (c.caption || "").toString().toLowerCase();
    return rc === cls || rn === cls || rc.indexOf(cls) >= 0
        || rn.indexOf(cls) >= 0 || cap.indexOf(cls) >= 0;
}}
var list = (typeof workspace.windowList === "function")
    ? workspace.windowList() : workspace.clientList();
for (var i = 0; i < list.length; ++i) {{
    var c = list[i];
    if (!matchWin(c)) continue;
    if (typeof workspace.activeWindow !== "undefined")
        workspace.activeWindow = c;
    else
        workspace.activeClient = c;
    break;
}}
"""
    path = None
    try:
        import tempfile
        fd, path = tempfile.mkstemp(prefix="fifine-kwin-", suffix=".js")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        sid = _run_out([
            qdbus, "org.kde.KWin", "/Scripting",
            "org.kde.kwin.Scripting.loadScript", path,
        ])
        if not sid.isdigit():
            return False
        base = f"/Scripting/Script{sid}"
        _run([qdbus, "org.kde.KWin", base, "org.kde.kwin.Script.run"],
             stderr=subprocess.DEVNULL)
        _run([qdbus, "org.kde.KWin", base, "org.kde.kwin.Script.stop"],
             stderr=subprocess.DEVNULL)
        _run([qdbus, "org.kde.KWin", "/Scripting",
              "org.kde.kwin.Scripting.unloadScript", sid],
             stderr=subprocess.DEVNULL)
        return True
    except Exception:
        log.debug("KWin activate failed", exc_info=True)
        return False
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _focus_app_window(hint: str) -> bool:
    """Best-effort focus a window by class/title (Chatterino, etc.)."""
    hint = (hint or "").strip()
    if not hint:
        return False
    # X11 / XWayland via xdotool.
    if _has("xdotool"):
        for flag in ("--class", "--classname", "--name"):
            ids = _run_out(["xdotool", "search", "--onlyvisible", flag, hint])
            wid = ids.splitlines()[0].strip() if ids else ""
            if wid.isdigit():
                _run(["xdotool", "windowactivate", "--sync", wid],
                     stderr=subprocess.DEVNULL)
                return True
    if _has("wmctrl"):
        # -xa matches WM_CLASS; fall back to title substring (-a).
        for args in (["wmctrl", "-xa", hint], ["wmctrl", "-a", hint]):
            try:
                r = subprocess.run(args, stderr=subprocess.DEVNULL,
                                   timeout=5, env=child_env())
                if r.returncode == 0:
                    return True
            except (OSError, subprocess.SubprocessError):
                pass
    # Hyprland.
    if _has("hyprctl"):
        out = _run_out(["hyprctl", "clients", "-j"])
        if out:
            try:
                import json
                clients = json.loads(out)
            except (json.JSONDecodeError, TypeError):
                clients = []
            want = hint.casefold()
            for c in clients if isinstance(clients, list) else []:
                cls = str(c.get("class") or "")
                title = str(c.get("title") or "")
                if want in cls.casefold() or want in title.casefold():
                    addr = c.get("address")
                    if addr:
                        _run(["hyprctl", "dispatch", "focuswindow",
                              f"address:{addr}"],
                             stderr=subprocess.DEVNULL)
                        return True
    # KDE Plasma Wayland (and X11) via KWin scripting.
    if _kwin_activate_class(hint):
        return True
    return False


def _chatterino_command(command: str = "/clip",
                        window: str = "chatterino") -> None:
    """Focus Chatterino and run a chat command (default: /clip).

    Chatterino's built-in ``/clip`` creates a Twitch clip of the current
    channel. We activate the Chatterino window, then type the command + Enter
    through the same keystroke backend as Type text / Hotkey.
    """
    cmd = (command or "/clip").strip() or "/clip"
    if not cmd.startswith("/"):
        cmd = "/" + cmd
    win = (window or "chatterino").strip() or "chatterino"
    if not KEY_TOOL:
        log.warning("Chatterino command needs xdotool / ydotool / wtype; "
                    "nothing was typed")
        return
    if not _focus_app_window(win):
        log.warning(
            "Chatterino window %r not found — open Chatterino (and focus a "
            "Twitch channel split) then press the key again", win)
        return
    # Give the compositor a beat to finish the focus change before typing.
    time.sleep(0.2)
    _type_text(cmd + "\n")


def _media(cmd: str) -> None:
    if HAS_PLAYERCTL:
        _run(["playerctl", cmd], stderr=subprocess.DEVNULL)
    else:
        log.warning("media control needs 'playerctl'")


def _play_sound(clip: str, file_path: str = "", volume: str = "80",
                context: ActionContext | None = None) -> None:
    from . import sounds
    sink, also_default = "", True
    if context is not None:
        try:
            sink, also_default = context.sound_output()
        except Exception as e:  # noqa: BLE001
            log.debug("sound_output unavailable: %s", e)
    sounds.play(clip or "bruh", file_path or "", volume or "80",
                sink=sink or "", also_default=bool(also_default))


SINK = "@DEFAULT_AUDIO_SINK@"

# Upper bound for a multi-action's per-step delay, matching the step editor's
# own QDoubleSpinBox range (gui/widgets.py). Enforced here too because the
# executor also reads hand-edited configs.
MAX_STEP_DELAY = 30.0


def _volume(cmd: str, step: str) -> None:
    try:
        pct = int(str(step if str(step).strip() else "5").strip().rstrip("%"))
    except ValueError:
        pct = 5
    # The GUI's "Step %" is free text. A negative one built "-20%+", which
    # wpctl's option parser reads as a flag rather than a value: stderr goes to
    # DEVNULL, so the key just did nothing. Clamp to a range that can only ever
    # be an argument. Floor at 0, NOT 1: a step of "0" is a deliberate no-op
    # (as it was before 0.12.0), and raising it to 1 turned every press of a
    # zero-step key into a real 1% nudge.
    pct = max(0, min(100, abs(pct)))
    if AUDIO == "pipewire":
        if cmd == "up":
            _run(["wpctl", "set-volume", "-l", "1.5", SINK, f"{pct}%+"])
        elif cmd == "down":
            _run(["wpctl", "set-volume", SINK, f"{pct}%-"])
        elif cmd == "mute":
            _run(["wpctl", "set-mute", SINK, "toggle"])
    elif AUDIO == "pulseaudio":
        s = "@DEFAULT_SINK@"
        if cmd == "up":
            _run(["pactl", "set-sink-volume", s, f"+{pct}%"])
        elif cmd == "down":
            _run(["pactl", "set-sink-volume", s, f"-{pct}%"])
        elif cmd == "mute":
            _run(["pactl", "set-sink-mute", s, "toggle"])
    else:
        log.warning("volume control needs pipewire (wpctl) or pulseaudio (pactl)")


# Must match model._iter_step_action_dicts: the import warning walks nested
# multi-steps only this deep, so anything the executor would run BELOW that
# depth would run without ever being listed in the "this config runs shell
# commands" dialog. Capping both at the same number keeps the promise.
MAX_STEP_DEPTH = 32


def _obs_conn(context: ActionContext | None) -> tuple[str, int, str] | None:
    """Resolve OBS host/port/password from the controller, or None."""
    if context is None:
        log.warning("OBS action needs a running controller (no connection settings)")
        return None
    try:
        host, port, password = context.obs_connection()
    except Exception as e:  # noqa: BLE001
        log.warning("OBS connection settings unavailable: %s", e)
        return None
    return host, port, password


def _obs_request(context: ActionContext | None, request_type: str,
                 request_data: dict | None = None) -> Optional[dict]:
    """Send one OBS request using the global Options connection settings."""
    from . import obs_ws
    conn = _obs_conn(context)
    if conn is None:
        return None
    host, port, password = conn
    return obs_ws.call(host, port, password, request_type, request_data)


# Scene-name aliases so chips like "BRB" / "Starting Soon" still hit common
# OBS scene titles (case and punctuation ignored).
_OBS_SCENE_ALIASES: dict[str, tuple[str, ...]] = {
    "starting soon": (
        "starting soon", "startingsoon", "soon", "intro", "be right back soon",
        "starting", "countdown",
    ),
    "brb": ("brb", "be right back", "be-right-back", "away", "brb."),
    "live": ("live", "main", "stream", "on air", "onair", "program"),
    "game capture": (
        "game capture", "game", "gaming", "gameplay", "game scene", "in game",
    ),
}

_OBS_MIC_ALIASES: tuple[str, ...] = (
    "mic/aux", "mic", "microphone", "mic aux", "analog mic", "usb mic",
    "headset", "voice",
)


def _norm_obs_name(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def _match_obs_name(wanted: str, candidates: list[str],
                    alias_groups: dict[str, tuple[str, ...]] | None = None
                    ) -> str | None:
    """Pick the best OBS scene/input name for a chip label / configured value."""
    wanted = (wanted or "").strip()
    if not wanted or not candidates:
        return None
    # Exact, then case-insensitive.
    for n in candidates:
        if n == wanted:
            return n
    low = wanted.casefold()
    for n in candidates:
        if n.casefold() == low:
            return n
    want_n = _norm_obs_name(wanted)
    for n in candidates:
        if _norm_obs_name(n) == want_n:
            return n
    # Alias group: wanted maps to a group; first candidate in that group wins.
    if alias_groups:
        group_keys = []
        for key, aliases in alias_groups.items():
            pool = (_norm_obs_name(key),) + tuple(_norm_obs_name(a) for a in aliases)
            if want_n in pool or any(want_n == a or a in want_n or want_n in a
                                     for a in pool if a):
                group_keys.append(key)
        for key in group_keys or ():
            aliases = alias_groups[key]
            norms = {_norm_obs_name(key), *(_norm_obs_name(a) for a in aliases)}
            for n in candidates:
                nn = _norm_obs_name(n)
                if nn in norms or any(a and (a in nn or nn in a) for a in norms):
                    return n
    # Substring fallback (e.g. "Game" → "Game Capture").
    for n in candidates:
        nl = n.casefold()
        if low in nl or nl in low:
            return n
    return None


def _resolve_obs_scene_name(context: ActionContext | None, wanted: str
                            ) -> str | None:
    """Map a chip scene label to a real OBS scene name when possible."""
    wanted = (wanted or "").strip()
    if not wanted:
        return None
    data = _obs_request(context, "GetSceneList")
    if data is None:
        # OBS unreachable — still try the configured name (same as before).
        return wanted
    names = [
        str(s.get("sceneName"))
        for s in (data.get("scenes") or [])
        if isinstance(s, dict) and s.get("sceneName")
    ]
    if not names:
        return wanted
    matched = _match_obs_name(wanted, names, _OBS_SCENE_ALIASES)
    if matched is None:
        log.warning("OBS: no scene matching %r (have: %s)",
                    wanted, ", ".join(names[:16]))
        return None
    if matched != wanted:
        log.info("OBS: resolved scene %r → %r", wanted, matched)
    return matched


def _resolve_obs_input_name(context: ActionContext | None, wanted: str
                            ) -> str | None:
    """Map Mic/Aux (etc.) to a real OBS input when names differ."""
    wanted = (wanted or "").strip()
    if not wanted:
        return None
    data = _obs_request(context, "GetInputList")
    if data is None:
        return wanted
    names = [
        str(i.get("inputName"))
        for i in (data.get("inputs") or [])
        if isinstance(i, dict) and i.get("inputName")
    ]
    if not names:
        return wanted
    aliases = {"mic/aux": _OBS_MIC_ALIASES, "mic": _OBS_MIC_ALIASES}
    matched = _match_obs_name(wanted, names, aliases)
    if matched is None and _norm_obs_name(wanted) in {
            _norm_obs_name(a) for a in _OBS_MIC_ALIASES}:
        matched = _match_obs_name("Mic/Aux", names, aliases)
    if matched is None:
        log.warning("OBS: no input matching %r (have: %s)",
                    wanted, ", ".join(names[:16]))
        return None
    if matched != wanted:
        log.info("OBS: resolved input %r → %r", wanted, matched)
    return matched


def _obs_scene(context: ActionContext | None, scene: str, *, preview: bool = False) -> None:
    scene = (scene or "").strip()
    if not scene:
        log.warning("OBS scene action has no scene name")
        return
    resolved = _resolve_obs_scene_name(context, scene)
    if not resolved:
        return
    req = "SetCurrentPreviewScene" if preview else "SetCurrentProgramScene"
    _obs_request(context, req, {"sceneName": resolved})


def _obs_transition(context: ActionContext | None) -> None:
    _obs_request(context, "TriggerStudioModeTransition")


def _obs_source(context: ActionContext | None, scene: str, source: str,
                state: str) -> None:
    scene = (scene or "").strip()
    source = (source or "").strip()
    state = (state or "toggle").strip().lower()
    if not scene or not source:
        log.warning("OBS source action needs both scene and source names")
        return
    resolved_scene = _resolve_obs_scene_name(context, scene)
    if not resolved_scene:
        return
    info = _obs_request(context, "GetSceneItemId",
                        {"sceneName": resolved_scene, "sourceName": source})
    if info is None:
        return
    item_id = info.get("sceneItemId")
    if item_id is None:
        log.warning("OBS: no scene item id for %r in scene %r",
                    source, resolved_scene)
        return
    if state == "toggle":
        cur = _obs_request(context, "GetSceneItemEnabled",
                           {"sceneName": resolved_scene, "sceneItemId": item_id})
        if cur is None:
            return
        enabled = not bool(cur.get("sceneItemEnabled", True))
    elif state == "show":
        enabled = True
    elif state == "hide":
        enabled = False
    else:
        log.warning("OBS source state must be show/hide/toggle, got %r", state)
        return
    _obs_request(context, "SetSceneItemEnabled",
                 {"sceneName": resolved_scene, "sceneItemId": item_id,
                  "sceneItemEnabled": enabled})


def _obs_output(context: ActionContext | None, kind: str, cmd: str) -> None:
    """kind is 'recording' or 'streaming'; cmd is start/stop/toggle."""
    cmd = (cmd or "toggle").strip().lower()
    table = {
        ("recording", "start"): "StartRecord",
        ("recording", "stop"): "StopRecord",
        ("recording", "toggle"): "ToggleRecord",
        ("streaming", "start"): "StartStream",
        ("streaming", "stop"): "StopStream",
        ("streaming", "toggle"): "ToggleStream",
    }
    req = table.get((kind, cmd))
    if not req:
        log.warning("OBS %s cmd must be start/stop/toggle, got %r", kind, cmd)
        return
    # Avoid no-op failures when already streaming/recording (OBS returns an
    # error for Start* while active, which looked like a broken chip).
    if cmd in ("start", "stop"):
        status_req = ("GetStreamStatus" if kind == "streaming"
                      else "GetRecordStatus")
        status = _obs_request(context, status_req)
        if status is not None:
            active = bool(status.get("outputActive"))
            if cmd == "start" and active:
                log.info("OBS %s already active — skipping start", kind)
                return
            if cmd == "stop" and not active:
                log.info("OBS %s already stopped — skipping stop", kind)
                return
    _obs_request(context, req)


def _obs_mute(context: ActionContext | None, input_name: str, state: str) -> None:
    input_name = (input_name or "").strip()
    state = (state or "toggle").strip().lower()
    if not input_name:
        log.warning("OBS mute action has no input name")
        return
    resolved = _resolve_obs_input_name(context, input_name)
    if not resolved:
        return
    if state == "toggle":
        _obs_request(context, "ToggleInputMute", {"inputName": resolved})
    elif state in ("mute", "unmute"):
        _obs_request(context, "SetInputMute",
                     {"inputName": resolved, "inputMuted": state == "mute"})
    else:
        log.warning("OBS mute state must be mute/unmute/toggle, got %r", state)


def execute(action, context: ActionContext | None = None,
            _depth: int = 0) -> None:
    """Execute a single Action. Never raises; logs failures."""
    t = action.type
    p = action.params
    try:
        if t == "none":
            return
        elif t == "launch_app":
            cmd = p.get("command", "").strip()
            if cmd:
                _popen_detached(cmd, shell=True)
        elif t == "run_command":
            cmd = p.get("command", "").strip()
            if cmd:
                _popen_detached(cmd, shell=True)
        elif t == "open_url":
            url = p.get("url", "").strip()
            if url:
                _popen_detached(["xdg-open", url])
        elif t == "hotkey":
            _send_hotkey(p.get("keys", ""))
        elif t == "text":
            # Optional trailing Return — useful for chat boxes where Enter
            # sends the message. Default is off so existing chips keep typing
            # only. A real newline is what _type_text turns into Return.
            text = p.get("text", "")
            if str(p.get("press_enter", "no")).lower() in ("yes", "true", "1", "on"):
                text = text + "\n"
            _type_text(text)
        elif t == "password":
            from . import secret_store
            pw = p.get("password") or (
                secret_store.get(p["secret_id"]) if p.get("secret_id") else "")
            if not pw:
                # secret_store.get returns None both for "no keyring backend"
                # and "keyring is locked", and the key used to type an empty
                # string either way with nothing to show for it. The user sees a
                # dead key and has no idea the secret was simply unavailable —
                # so name the likely cause instead of typing nothing silently.
                if p.get("secret_id"):
                    log.warning(
                        "password key: no secret available for %s — the login "
                        "keyring is locked, empty, or has no backend installed. "
                        "Nothing was typed.", p["secret_id"])
                else:
                    log.warning("password key has no password set; nothing was typed")
                return
            _type_text(pw)
        elif t == "media":
            _media(p.get("cmd", "play-pause"))
        elif t == "volume":
            _volume(p.get("cmd", "up"), p.get("step", "5"))
        elif t == "play_sound":
            _play_sound(p.get("clip", "bruh"), p.get("file", ""),
                        p.get("volume", "80"), context)
        elif t == "close_app":
            _close_app(p.get("target", ""))
        elif t == "chatterino":
            _chatterino_command(p.get("command", "/clip"),
                                p.get("window", "chatterino"))
        elif t == "monitor":
            return    # display-only key: pressing it does nothing
        elif t == "sleep_screen" and context:
            context.sleep_screen()
        elif t == "next_profile" and context:
            context.next_profile()
        elif t == "prev_profile" and context:
            context.prev_profile()
        elif t == "next_page" and context:
            context.next_page()
        elif t == "prev_page" and context:
            context.prev_page()
        elif t == "goto_page" and context:
            try:
                _page = int(float(str(p.get("page", "1")).strip().replace(",", ".")))
            except (TypeError, ValueError, OverflowError):
                _page = 1        # a bad value must not silently drop the action
                                 # (OverflowError: int(float("1e999")) -> inf)
            context.goto_page(_page - 1)
        elif t == "switch_profile" and context:
            context.switch_profile(p.get("profile_id", ""))
        elif t == "brightness" and context:
            mode = p.get("mode", "set")
            # `or "10"` would turn a JSON numeric 0 into 10, because 0 is
            # falsy — so "brightness set 0" silently became 10. Only a missing
            # or blank value takes the default.
            raw = p.get("value", "10")
            if str(raw).strip() in ("", "None"):
                val = 10
            else:
                try:
                    # Guard + accept a comma decimal (fr_FR): unlike _volume,
                    # this had no local guard, so any non-integer text escaped
                    # to the outer catch and the key became a silent no-op.
                    val = int(float(str(raw).strip().replace(",", ".")))
                except (TypeError, ValueError, OverflowError):
                    val = 10     # OverflowError: int(float("1e999")) -> inf
            if mode == "set":
                context.set_brightness(val)
            elif mode == "up":
                context.adjust_brightness(abs(val))
            elif mode == "down":
                context.adjust_brightness(-abs(val))
        elif t == "obs_scene":
            _obs_scene(context, p.get("scene", ""))
        elif t == "obs_preview_scene":
            _obs_scene(context, p.get("scene", ""), preview=True)
        elif t == "obs_transition":
            _obs_transition(context)
        elif t == "obs_source":
            _obs_source(context, p.get("scene", ""), p.get("source", ""),
                        p.get("state", "toggle"))
        elif t == "obs_recording":
            _obs_output(context, "recording", p.get("cmd", "toggle"))
        elif t == "obs_streaming":
            _obs_output(context, "streaming", p.get("cmd", "toggle"))
        elif t == "obs_mute":
            _obs_mute(context, p.get("input", ""), p.get("state", "toggle"))
        elif t == "multi":
            from .model import Action as _A
            if _depth >= MAX_STEP_DEPTH:
                log.warning("multi-action nested deeper than %d levels; "
                            "not running the rest", MAX_STEP_DEPTH)
                return
            for step in p.get("steps", []):
                # A single malformed step (non-dict, or a bad "delay" like
                # "0.5s") must not abort the remaining steps: the outer guard
                # would catch it once and drop the whole sequence silently.
                if not isinstance(step, dict):
                    continue
                sub = _A.from_dict(step.get("action", step))
                execute(sub, context, _depth + 1)
                try:
                    delay = float(step.get("delay", 0) or 0)
                except (TypeError, ValueError):
                    delay = 0.0
                # Clamp, for two reasons. A hand-edited delay above ~9.2e18
                # makes time.sleep raise OverflowError, which escapes to the
                # outer guard and drops every remaining step — defeating the
                # comment above. And this sleep holds the single action worker,
                # so an unbounded delay makes the WHOLE deck unresponsive:
                # every other key press queues up behind it and then replays in
                # a burst. MAX_STEP_DELAY matches the step editor's own limit,
                # so nothing a user can build in the GUI is affected.
                if delay > MAX_STEP_DELAY:
                    log.warning("multi step delay %.6g s clamped to %g s; the "
                                "deck cannot answer any other key while it "
                                "waits", delay, MAX_STEP_DELAY)
                    delay = MAX_STEP_DELAY
                if delay > 0:
                    time.sleep(delay)
        else:
            log.warning("unhandled or context-less action: %s", t)
    except Exception as e:  # actions must never crash the reader thread
        log.error("'%s' failed: %s", t, e)


def environment_summary() -> str:
    return (f"session={'wayland' if IS_WAYLAND else 'x11'} "
            f"audio={AUDIO or 'none'} keytool={KEY_TOOL or 'none'} "
            f"playerctl={'yes' if HAS_PLAYERCTL else 'no'} "
            f"obs=ws5"
            + (" [snap]" if IN_SNAP else ""))


def snap_usb_hint() -> Optional[str]:
    """Guidance for granting a snap access to the USB device.

    Returns None unless running as a snap. The deck is driven over /dev/hidraw:
    - classic snap  -> can open it directly, but needs the host udev rule (a snap
      cannot install one); the rule is bundled at $SNAP/udev/70-fifine-deck.rules.
    - strict snap   -> hidraw cannot be granted at all; this build should not be
      used for device control (kept for completeness).
    """
    if not IN_SNAP:
        return None
    name = os.environ.get("SNAP_NAME", "fifine-control-deck")
    if IN_SNAP_CLASSIC:
        rule = os.path.join(os.environ.get("SNAP", ""), "udev", "70-fifine-deck.rules")
        return (
            "The deck is controlled over /dev/hidraw, which needs a udev rule so "
            "this snap can open it (a snap can't install the rule itself).\n\n"
            "If your deck is plugged in but not detected, run this once, then "
            "unplug/replug the device:\n\n"
            f"    sudo cp {rule} /etc/udev/rules.d/\n"
            "    sudo udevadm control --reload-rules && sudo udevadm trigger\n\n"
            "Tip: the .deb / PPA build installs this rule for you."
        )
    return (
        "This is the strict-confinement snap, which cannot access /dev/hidraw and "
        "so cannot control the deck. Install the classic snap, or the .deb / PPA:\n\n"
        "    sudo add-apt-repository ppa:zoutmax/fifine\n"
        "    sudo apt install fifine-control-deck\n\n"
        f"(For reference, USB interfaces on this build: "
        f"`sudo snap connect {name}:raw-usb`, `:hardware-observe`.)"
    )


def can_install_udev_rule() -> bool:
    """True if the one-click 'enable device access' path is available.

    Only the classic snap ships the bundled rule + helper and runs unconfined
    enough to call pkexec.
    """
    if not IN_SNAP_CLASSIC:
        return False
    helper = os.path.join(os.environ.get("SNAP", ""), "bin", "fifine-install-udev-rule")
    return os.path.exists(helper)


def install_udev_rule_pkexec() -> tuple[bool, str]:
    """Install the bundled udev rule as root via pkexec (graphical auth prompt).

    A snap can't install a udev rule itself, so the classic snap ships the rule
    and a small helper and elevates via polkit. Returns (ok, message); a
    non-zero exit means the user cancelled the auth dialog or it failed.
    """
    if not can_install_udev_rule():
        return (False, "The one-click installer is only available in the classic snap.")
    helper = os.path.join(os.environ["SNAP"], "bin", "fifine-install-udev-rule")
    # Call pkexec by absolute path — the snap's PATH may not include it, and the
    # real setuid binary lives at /usr/bin/pkexec (a symlink is fine too).
    pkexec = "/usr/bin/pkexec" if os.path.exists("/usr/bin/pkexec") else (
        shutil.which("pkexec") or "pkexec")
    try:
        r = subprocess.run(
            [pkexec, helper],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        return (False, "pkexec is not available on this system.")
    except subprocess.TimeoutExpired:
        return (False, "Timed out waiting for authentication.")
    if r.returncode == 0:
        return (True, "Device-access rule installed. Reconnecting to the deck…")
    if r.returncode in (126, 127):   # pkexec: dismissed / not authorized
        return (False, "Authentication was cancelled.")
    return (False, (r.stderr or r.stdout or "Could not install the rule.").strip())
