#!/usr/bin/env python3
"""Fetch/convert assets/sounds/french_meme_song.wav from MyInstants."""
from __future__ import annotations
import shutil, subprocess, sys, tempfile, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "sounds" / "french_meme_song.wav"
URL = "https://www.myinstants.com/media/sounds/french-meme-song.mp3"
REFERER = "https://www.myinstants.com/en/instant/french-meme-song-55813/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
def main() -> int:
    if not shutil.which("sox"):
        print("sox is required", file=sys.stderr); return 1
    with tempfile.TemporaryDirectory(prefix="fifine-french-meme-") as tmp:
        mp3 = Path(tmp) / "french-meme-song.mp3"
        req = urllib.request.Request(URL, headers={"User-Agent": UA, "Referer": REFERER})
        with urllib.request.urlopen(req, timeout=30) as response: mp3.write_bytes(response.read())
        OUT.parent.mkdir(parents=True, exist_ok=True)
        subprocess.check_call(["sox", str(mp3), "-r", "22050", "-c", "1", "-b", "16", str(OUT), "gain", "-n", "-4", "fade", "0.005"])
    print(f"wrote {OUT.relative_to(ROOT)} from {URL}"); return 0
if __name__ == "__main__": raise SystemExit(main())
