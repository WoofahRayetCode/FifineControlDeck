"""Bundled sound clips and playback for the Play sound action.

Clips live in assets/sounds/ (WAV). Playback prefers PipeWire/Pulse helpers
so overlapping keypresses mix naturally; each play is a detached process.
"""
from __future__ import annotations

import json
import logging
import os
import random
import shutil
import subprocess
from typing import Optional

log = logging.getLogger(__name__)

SOUNDS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "sounds")
SOUNDS_INDEX = os.path.join(SOUNDS_DIR, "index.json")

# Special clip ids that are not files.
RANDOM_CLIP_IDS = {
    "random": "any built-in clip",
    "random_funny": "Funny / Farts / stingers",
    "random_fart": "Farts category",
    "random_music": "Music category",
}

_CLIP_CACHE: list[dict] | None = None


def _load_index() -> dict:
    if not os.path.exists(SOUNDS_INDEX):
        return {}
    try:
        with open(SOUNDS_INDEX, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read sound index: %s", e)
        return {}


def list_clips() -> list[dict]:
    """Return [{name, file, label, category, path}], sorted by category/label."""
    global _CLIP_CACHE
    if _CLIP_CACHE is not None:
        return list(_CLIP_CACHE)
    items = []
    for name, meta in _load_index().items():
        if not isinstance(meta, dict):
            continue
        fname = meta.get("file") or f"{name}.wav"
        path = os.path.join(SOUNDS_DIR, fname)
        items.append({
            "name": name,
            "file": fname,
            "label": meta.get("label", name),
            "category": meta.get("category", "Other"),
            "path": path if os.path.exists(path) else "",
        })
    items.sort(key=lambda x: (x["category"], x["label"]))
    _CLIP_CACHE = items
    return list(items)


def clip_names() -> list[str]:
    return [c["name"] for c in list_clips() if c.get("path")]


def clip_choice_values() -> list[str]:
    """Values for the action editor combo (builtins + random + custom)."""
    names = clip_names()
    return names + list(RANDOM_CLIP_IDS.keys()) + ["custom"]


def resolve_clip(clip: str, file_path: str = "") -> Optional[str]:
    """Resolve a clip id or custom path to an absolute audio file path."""
    clip = (clip or "").strip()
    file_path = (file_path or "").strip()

    if clip == "custom" or (not clip and file_path):
        if file_path and os.path.isfile(file_path):
            return os.path.abspath(file_path)
        log.warning("play_sound: custom file missing or unset (%r)", file_path)
        return None

    if clip in RANDOM_CLIP_IDS:
        pool = list_clips()
        if clip == "random_fart":
            pool = [c for c in pool if c["category"] == "Farts"]
        elif clip == "random_music":
            pool = [c for c in pool if c["category"] == "Music"]
        elif clip == "random_funny":
            pool = [c for c in pool
                    if c["category"] in ("Funny", "Farts", "Stinger")]
        pool = [c for c in pool if c.get("path")]
        if not pool:
            log.warning("play_sound: no clips available for %s", clip)
            return None
        return random.choice(pool)["path"]

    for c in list_clips():
        if c["name"] == clip and c.get("path"):
            return c["path"]

    # Allow a bare filename or absolute path in the clip field.
    if file_path and os.path.isfile(file_path):
        return os.path.abspath(file_path)
    if clip and os.path.isfile(clip):
        return os.path.abspath(clip)
    # lib-style: look under sounds dir
    if clip:
        cand = os.path.join(SOUNDS_DIR, clip if clip.endswith(".wav") else f"{clip}.wav")
        if os.path.isfile(cand):
            return cand
    log.warning("play_sound: unknown clip %r", clip)
    return None


def _player_cmd(path: str, volume_pct: int) -> Optional[list[str]]:
    """Build a detached player argv. Overlapping plays = multiple processes."""
    vol = max(1, min(100, int(volume_pct))) / 100.0
    if shutil.which("pw-play"):
        return ["pw-play", f"--volume={vol:.3f}", path]
    if shutil.which("paplay"):
        # paplay volume is 0..65536 linear
        return ["paplay", f"--volume={int(vol * 65536)}", path]
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                "-volume", str(int(vol * 100)), path]
    if shutil.which("mpv"):
        return ["mpv", "--no-video", "--really-quiet",
                f"--volume={int(vol * 100)}", path]
    return None


def play(clip: str = "bleep", file_path: str = "", volume: str | int = 80) -> bool:
    """Play a clip (or custom file). Returns True if a player was started.

    Never raises. Multiple calls overlap (mix) by design.
    """
    try:
        vol_i = int(float(str(volume).strip().rstrip("%").replace(",", ".")))
    except (TypeError, ValueError):
        vol_i = 80
    vol_i = max(1, min(100, abs(vol_i)))

    path = resolve_clip(clip, file_path)
    if not path:
        return False
    argv = _player_cmd(path, vol_i)
    if not argv:
        log.warning(
            "play_sound needs pw-play, paplay, ffplay, or mpv — nothing played")
        return False
    try:
        # Detached so overlapping keypresses mix; use a clean env when the
        # app is running from AppImage/snap (same idea as actions.child_env).
        from .actions import child_env
        subprocess.Popen(
            argv, start_new_session=True, env=child_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("play_sound failed: %s", e)
        return False
