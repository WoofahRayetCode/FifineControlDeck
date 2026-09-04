#!/usr/bin/env python3
"""Fetch/convert assets/sounds/zelda_item_get.wav from MyInstants.

Source:
  https://www.myinstants.com/en/instant/zelda-item-get/
  https://www.myinstants.com/media/sounds/139-item-catch.mp3

Requires sox on PATH.

  python3 tools/make_zelda_item_get.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "sounds" / "zelda_item_get.wav"
MP3_URL = "https://www.myinstants.com/media/sounds/139-item-catch.mp3"
REFERER = "https://www.myinstants.com/en/instant/zelda-item-get/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def main() -> int:
    if not shutil.which("sox"):
        print("sox is required", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fifine-zelda-") as tmp:
        mp3 = Path(tmp) / "zelda-item-get.mp3"
        req = urllib.request.Request(
            MP3_URL,
            headers={"User-Agent": UA, "Referer": REFERER,
                     "Accept": "audio/mpeg,audio/*;q=0.9,*/*;q=0.8"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            mp3.write_bytes(resp.read())
        subprocess.check_call([
            "sox", str(mp3), "-r", "22050", "-c", "1", "-b", "16", str(OUT),
            "gain", "-n", "-4",
            "fade", "0.005",
        ])
    print(f"wrote {OUT.relative_to(ROOT)} from {MP3_URL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
