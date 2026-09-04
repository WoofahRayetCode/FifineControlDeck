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


def list_sinks() -> list[tuple[str, str]]:
    """Return [(sink_name, description), ...] from Pulse/PipeWire.

    Empty on failure. Names are what ``pw-play --target`` / ``paplay --device``
    accept (e.g. ``easyeffects_sink``, ``alsa_output.…``).
    """
    if shutil.which("pactl"):
        try:
            raw = subprocess.check_output(
                ["pactl", "-f", "json", "list", "sinks"],
                stderr=subprocess.DEVNULL, timeout=5, text=True)
            data = json.loads(raw)
            out: list[tuple[str, str]] = []
            if isinstance(data, list):
                for s in data:
                    if not isinstance(s, dict):
                        continue
                    name = str(s.get("name") or "").strip()
                    if not name:
                        continue
                    desc = (str(s.get("description") or "").strip()
                            or str((s.get("properties") or {})
                                   .get("node.description") or "").strip()
                            or name)
                    out.append((name, desc))
            if out:
                return out
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError,
                TypeError, ValueError):
            pass
        # Fallback: short listing (id name driver …).
        try:
            raw = subprocess.check_output(
                ["pactl", "list", "short", "sinks"],
                stderr=subprocess.DEVNULL, timeout=5, text=True)
            out = []
            for line in raw.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1].strip():
                    name = parts[1].strip()
                    out.append((name, name))
            return out
        except (OSError, subprocess.SubprocessError):
            pass
    return []


def _player_cmd(path: str, volume_pct: int,
                sink: str = "") -> Optional[list[str]]:
    """Build a detached player argv. Overlapping plays = multiple processes.

    ``sink`` is a PipeWire/Pulse sink name (empty = system default).
    """
    vol = max(1, min(100, int(volume_pct))) / 100.0
    sink = (sink or "").strip()
    if shutil.which("pw-play"):
        argv = ["pw-play", f"--volume={vol:.3f}"]
        if sink:
            argv += ["--target", sink]
        argv.append(path)
        return argv
    if shutil.which("paplay"):
        # paplay volume is 0..65536 linear
        argv = ["paplay", f"--volume={int(vol * 65536)}"]
        if sink:
            argv += [f"--device={sink}"]
        argv.append(path)
        return argv
    if shutil.which("ffplay"):
        # ffplay has no Pulse device flag; PULSE_SINK is applied in _spawn.
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                "-volume", str(int(vol * 100)), path]
    if shutil.which("mpv"):
        argv = ["mpv", "--no-video", "--really-quiet",
                f"--volume={int(vol * 100)}"]
        if sink:
            argv.append(f"--audio-device=pipewire/{sink}")
        argv.append(path)
        return argv
    return None


def _spawn_player(argv: list[str], *, sink: str = "") -> bool:
    """Start one detached player process. Returns True on success."""
    try:
        from .actions import child_env
        env = child_env()
        # ffplay (and some Pulse clients) honour PULSE_SINK when --device is
        # unavailable. Harmless for pw-play which already got --target.
        if sink:
            env = dict(env)
            env["PULSE_SINK"] = sink
        subprocess.Popen(
            argv, start_new_session=True, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("play_sound failed: %s", e)
        return False


def play(clip: str = "bruh", file_path: str = "", volume: str | int = 80,
         sink: str = "", also_default: bool = True) -> bool:
    """Play a clip (or custom file). Returns True if a player was started.

    Never raises. Multiple calls overlap (mix) by design.

    ``sink`` routes to a specific PipeWire/Pulse sink so OBS can capture
    soundboard audio (pick a virtual sink / the device OBS monitors). When
    ``also_default`` is True and ``sink`` is set, a second play goes to the
    system default so you still hear the clip on headphones.
    """
    try:
        vol_i = int(float(str(volume).strip().rstrip("%").replace(",", ".")))
    except (TypeError, ValueError):
        vol_i = 80
    vol_i = max(1, min(100, abs(vol_i)))

    path = resolve_clip(clip, file_path)
    if not path:
        return False
    sink = (sink or "").strip()
    targets: list[str] = [sink] if sink else [""]
    if sink and also_default:
        targets.append("")  # default output as well

    started = False
    for target in targets:
        argv = _player_cmd(path, vol_i, target)
        if not argv:
            log.warning(
                "play_sound needs pw-play, paplay, ffplay, or mpv — nothing played")
            return False
        if _spawn_player(argv, sink=target):
            started = True
    return started
