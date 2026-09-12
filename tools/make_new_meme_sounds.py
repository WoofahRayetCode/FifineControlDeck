#!/usr/bin/env python3
"""Fetch and convert the additional MyInstants meme clips to WAV files."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOUNDS = {
    "du_bist_gut_genug": ("du-bist-gut-genug.mp3", "du-bist-gut-genug-22336"),
    "directed_by_robert_weide": ("directed-by-robert-b_voI2Z4T.mp3", "directed-by-robert-b-weide-451"),
    "okay_lets_go": ("meme-okay-lets-go.mp3", "okay-lets-go-99131"),
    "rat_dance_music": ("rat-dance-music.mp3", "rat-dance-music-93451"),
    "metal_gear_alert": ("metal-gear-alert-sound-effect_XKoHReZ.mp3", "metal-gear-alert-sound-effect-82026"),
    "gas_gas_gas": ("gas-gas-gaslqshort.mp3", "gas-gas-gas-manuel-short-31840"),
    "wet_fart": ("wet-fart-meme.mp3", "wet-fart-meme-47937"),
    "gegagedigedagedago": ("gegagedigedagedago-full.mp3", "gegagedigedagedago-full-74146"),
    "sad_violin": ("meme-violin-sad-violin.mp3", "meme-violin-sad-violin-1460"),
    "e_meme": ("its-in-the-game_TyOFKRF.mp3", "e-meme-98363"),
    "deja_vu_fade": ("deja-vu-fade.mp3", "deja-vu-fade-14940"),
    "samsung_notification": ("yt1s_nijLeKo.mp3", "samsung-notification-234223243-63614"),
    "hello_meme": ("hello-meme.mp3", "hello-meme-74584"),
    "ah_shit_here_we_go_again": ("gta-san-andreas-ah-shit-here-we-go-again_PHjnAqj.mp3", "ah-shit-here-we-go-again-81443"),
    "shooting_stars": ("fat-man-does-amazing-dive-shooting-stars_2.mp3", "shooting-stars-meme-3743"),
    "doom_music": ("doom-music.mp3", "doom-music-5982"),
    "oblivion_npc_theme": ("oblivion-npc-theme.mp3", "oblivion-npc-theme-63614"),
    "meme_67": ("67-meme_cdLcL5q.mp3", "67-meme-3268"),
    "butter_dog": ("megumins.mp3", "butter-dog-95803"),
    "arabic_nokia": ("arabic-nokia.mp3", "arabic-nokia-46441"),
}
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def main() -> int:
    if not shutil.which("sox"):
        print("sox is required", file=sys.stderr)
        return 1
    out_dir = ROOT / "assets" / "sounds"
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fifine-new-memes-") as temp:
        temp_dir = Path(temp)
        for name, (filename, slug) in SOUNDS.items():
            mp3 = temp_dir / filename
            url = f"https://www.myinstants.com/media/sounds/{filename}"
            referer = f"https://www.myinstants.com/en/instant/{slug}/"
            request = urllib.request.Request(url, headers={
                "User-Agent": UA, "Referer": referer,
                "Accept": "audio/mpeg,audio/*;q=0.9,*/*;q=0.8",
            })
            with urllib.request.urlopen(request, timeout=30) as response:
                mp3.write_bytes(response.read())
            output = out_dir / f"{name}.wav"
            subprocess.check_call([
                "sox", str(mp3), "-r", "22050", "-c", "1", "-b", "16",
                str(output), "gain", "-n", "-4", "fade", "0.005",
            ])
            print(f"wrote {output.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
