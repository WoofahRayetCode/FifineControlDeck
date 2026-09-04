"""
Key image rendering. Composes a background colour, an optional icon, and an
optional text label into a square key image. Used both to push JPEGs to the
device and to draw the live preview in the GUI.
"""
from __future__ import annotations

import io
import logging
import os
from functools import lru_cache

log = logging.getLogger(__name__)

from PIL import Image, ImageDraw, ImageFont, ImageEnhance

# Cover the common layouts: Debian/Ubuntu (truetype/…), Fedora
# (dejavu/…, liberation-fonts/…), Arch (TTF/…). Missing all of them silently
# degrades to Pillow's tiny bitmap font.
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]


@lru_cache(maxsize=64)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _hex(color: str, fallback=(16, 16, 32)):
    try:
        c = color.lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return fallback


def chat_body_metrics(size: int, text: str = "", *, header: bool = False
                      ) -> tuple[int, int, int]:
    """Return ``(text_width, view_width, font_size)`` for chat body layout.

    Used by the controller to decide whether a tile needs marquee scrolling.
    """
    pad = max(2, size // 20)
    max_w = size - 2 * pad
    body = " ".join((text or "").split())
    body_font = _font(max(8, size // 9))
    # textlength is exact for a single line; empty body is zero.
    try:
        width = int(ImageDraw.Draw(Image.new("RGB", (1, 1)))
                    .textlength(body, font=body_font)) if body else 0
    except Exception:
        width = len(body) * max(5, size // 12)
    return width, max_w, max(8, size // 9)


def render_chat_tile(
    size: int,
    user: str = "",
    text: str = "",
    user_color: str = "#9147ff",
    bg_color: str = "#0e0e10",
    header: str = "",
    scroll_px: int = 0,
) -> Image.Image:
    """Compact chat line for one physical key (Twitch chat page mode).

    When the body is wider than the chip, ``scroll_px`` advances a horizontal
    marquee so the full message can be read over time.
    """
    img = Image.new("RGB", (size, size), _hex(bg_color, (14, 14, 16)))
    draw = ImageDraw.Draw(img)
    pad = max(2, size // 20)
    max_w = size - 2 * pad

    if header:
        font = _font(max(9, size // 7))
        draw.text((pad, pad), header[:18], fill=(145, 71, 255), font=font)
        body_top = pad + int(size * 0.28)
        body = " ".join((text or "waiting for chat…").split())
        body_font = _font(max(8, size // 9))
        try:
            body_w = int(draw.textlength(body, font=body_font)) if body else 0
        except Exception:
            body_w = 0
        if body_w > max_w:
            _paste_marquee(img, body, body_font, (230, 230, 230),
                           pad, body_top, max_w, scroll_px)
        else:
            lines = _wrap(draw, body, body_font, max_w)
            line_h = max(10, size // 9 + 2)
            for i, line in enumerate(lines):
                draw.text((pad, body_top + i * line_h), line,
                          fill=(230, 230, 230), font=body_font)
        return img

    name = " ".join((user or "").split())[:16]
    body = " ".join((text or "").split())
    name_font = _font(max(8, size // 8))
    body_font = _font(max(8, size // 9))
    draw.text((pad, pad), name or "…", fill=_hex(user_color, (145, 71, 255)),
              font=name_font)
    name_h = int(size * 0.28)
    body_y = pad + name_h
    try:
        body_w = int(draw.textlength(body, font=body_font)) if body else 0
    except Exception:
        body_w = 0
    if body and body_w > max_w:
        _paste_marquee(img, body, body_font, (235, 235, 235),
                       pad, body_y, max_w, scroll_px)
    elif body:
        lines = _wrap(draw, body, body_font, max_w)
        line_h = max(10, size // 9 + 2)
        for i, line in enumerate(lines):
            draw.text((pad, body_y + i * line_h), line,
                      fill=(235, 235, 235), font=body_font)
    return img


def _cover_crop_rgb(src: Image.Image, width: int, height: int) -> Image.Image | None:
    """Scale ``src`` to cover ``width``×``height`` and center-crop."""
    if width <= 0 or height <= 0:
        return src.convert("RGB") if src.mode != "RGB" else src
    rgb = src.convert("RGB")
    sw, sh = rgb.size
    if sw <= 0 or sh <= 0:
        return None
    scale = max(width / sw, height / sh)
    nw = max(1, int(round(sw * scale)))
    nh = max(1, int(round(sh * scale)))
    resized = rgb.resize((nw, nh), Image.Resampling.LANCZOS)
    left = max(0, (nw - width) // 2)
    top = max(0, (nh - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _slice_canvas(canvas: Image.Image, cols: int, rows: int,
                  key_size: int) -> dict[int, Image.Image]:
    tiles: dict[int, Image.Image] = {}
    for r in range(rows):
        for c in range(cols):
            idx = r * cols + c + 1
            left = c * key_size
            top = r * key_size
            tiles[idx] = canvas.crop(
                (left, top, left + key_size, top + key_size))
    return tiles


def load_photo_cover(path: str, width: int, height: int) -> Image.Image | None:
    """Load ``path`` and return an RGB image cover-cropped to ``width``×``height``.

    Returns None if the file is missing or undecodable. Animated images use
    the first frame only — see :func:`load_page_gif_animation` for playback.
    """
    path = os.path.expanduser((path or "").strip())
    if not path or not os.path.isfile(path):
        return None
    try:
        src = Image.open(path)
        src.seek(0)
    except Exception as e:
        log.warning("photo %s could not be decoded: %s", path, e)
        return None
    return _cover_crop_rgb(src, width, height)


def slice_photo_tiles(
    path: str,
    cols: int,
    rows: int,
    key_size: int,
) -> dict[int, Image.Image]:
    """Split a photo into a ``cols``×``rows`` grid of ``key_size`` tiles.

    Keys are numbered 1..cols*rows in reading order (left→right, top→bottom),
    matching the GUI key grid. Returns an empty dict on failure.
    """
    cols = max(1, int(cols))
    rows = max(1, int(rows))
    key_size = max(1, int(key_size))
    canvas = load_photo_cover(path, cols * key_size, rows * key_size)
    if canvas is None:
        return {}
    return _slice_canvas(canvas, cols, rows, key_size)


def load_page_gif_animation(
    path: str,
    cols: int,
    rows: int,
    key_size: int,
) -> list[tuple[dict[int, Image.Image], int]]:
    """Decode an animated GIF/WebP into per-frame key tile maps.

    Returns a list of ``(tiles, delay_ms)``. Empty on failure. A still image
    (or single-frame GIF) returns one entry — callers may treat that as static.
    """
    from PIL import ImageSequence

    path = os.path.expanduser((path or "").strip())
    if not path or not os.path.isfile(path):
        return []
    cols = max(1, int(cols))
    rows = max(1, int(rows))
    key_size = max(1, int(key_size))
    canvas_w, canvas_h = cols * key_size, rows * key_size

    try:
        im = Image.open(path)
    except Exception as e:
        log.warning("gif/page image %s could not be opened: %s", path, e)
        return []

    n_frames = int(getattr(im, "n_frames", 1) or 1)
    # Composite frames so GIF disposal / partial updates look correct.
    canvas = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out: list[tuple[dict[int, Image.Image], int]] = []
    try:
        for frame in ImageSequence.Iterator(im):
            delay = int(frame.info.get("duration", 100) or 100)
            delay = max(40, min(delay, 5000))
            fr = frame.convert("RGBA")
            disposal = frame.info.get("disposal", 1)
            try:
                disposal = int(disposal)
            except (TypeError, ValueError):
                disposal = 1
            if disposal == 2:
                canvas = Image.new("RGBA", im.size, (0, 0, 0, 0))
            # Most GIF frames are full-canvas after convert; paste with alpha.
            canvas.paste(fr, (0, 0), fr)
            covered = _cover_crop_rgb(canvas, canvas_w, canvas_h)
            if covered is None:
                continue
            out.append((_slice_canvas(covered, cols, rows, key_size), delay))
    except Exception as e:
        log.warning("gif/page image %s decode failed: %s", path, e)
        return []

    if not out and n_frames >= 1:
        tiles = slice_photo_tiles(path, cols, rows, key_size)
        if tiles:
            return [(tiles, 100)]
    return out


def badge_photo_back_tile(tile: Image.Image) -> Image.Image:
    """Draw a small double-tap-back hint on the top-left photo tile."""
    img = tile.convert("RGB").copy()
    draw = ImageDraw.Draw(img)
    size = img.size[0]
    pad = max(2, size // 16)
    font = _font(max(8, size // 8))
    label = "2× ←"
    # Dark pill behind the hint for contrast on any photo.
    bb = draw.textbbox((0, 0), label, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    box = (pad - 2, pad - 2, pad + tw + 4, pad + th + 4)
    draw.rectangle(box, fill=(0, 0, 0))
    draw.text((pad, pad), label, fill=(255, 255, 255), font=font)
    return img


def _paste_marquee(img: Image.Image, text: str, font, color, x0: int, y: int,
                   view_w: int, scroll_px: int) -> None:
    """Blit a looping horizontal marquee of ``text`` into ``img`` at (x0, y)."""
    draw = ImageDraw.Draw(img)
    try:
        text_w = int(draw.textlength(text, font=font))
    except Exception:
        text_w = len(text) * 6
    if text_w <= view_w:
        draw.text((x0, y), text, fill=color, font=font)
        return
    gap = max(view_w // 3, 16)
    period = text_w + gap
    offset = int(scroll_px) % period if period else 0
    # Font pixel size is a reasonable strip height.
    try:
        ascent, descent = font.getmetrics()
        strip_h = max(ascent + descent + 2, getattr(font, "size", 12) + 4)
    except Exception:
        strip_h = getattr(font, "size", 12) + 4
    strip = Image.new("RGB", (period + view_w + 2, strip_h), img.getpixel((0, 0)))
    sdraw = ImageDraw.Draw(strip)
    sdraw.text((0, 0), text, fill=color, font=font)
    sdraw.text((period, 0), text, fill=color, font=font)
    window = strip.crop((offset, 0, offset + view_w, strip_h))
    img.paste(window, (x0, y))


def render_key(
    size: int,
    label: str = "",
    icon_path: str = "",
    bg_color: str = "#101020",
    text_color: str = "#ffffff",
    pressed: bool = False,
) -> Image.Image:
    """Return an RGB PIL image (size x size), upright (no device rotation yet).
    If pressed, brighten the whole key as a simple press flash."""
    from . import assets
    icon_path = assets.resolve_icon(icon_path)
    img = Image.new("RGB", (size, size), _hex(bg_color))

    # Whether an icon was actually PAINTED — not merely configured. The text
    # layout below shrinks the font and bottom-aligns it to make room for an
    # icon; keying that off the path meant a deleted or undecodable icon left
    # the label small and jammed against the bottom edge of an empty key.
    drew_icon = False
    if icon_path and os.path.exists(icon_path):
        try:
            icon = Image.open(icon_path).convert("RGBA")
            pad = int(size * 0.08)
            box = size - 2 * pad
            # If there's also a label, leave room at the bottom.
            if label:
                box = int(box * 0.72)
            icon.thumbnail((box, box), Image.Resampling.LANCZOS)
            x = (size - icon.width) // 2
            y = pad if label else (size - icon.height) // 2
            img.paste(icon, (x, y), icon)
            drew_icon = True
        except Exception as e:
            # Broad on purpose: a bad icon must degrade to a bare-background
            # key, never break the whole frame render. UnidentifiedImageError
            # and truncated files raise OSError, but a valid-but-huge image
            # trips Pillow's DecompressionBombError, which is NOT an OSError.
            # But SAY so — a silently blank key (e.g. an SVG, which Pillow
            # cannot decode) was indistinguishable from a missing icon.
            log.warning("icon %s could not be decoded: %s", icon_path, e)

    if label:
        # Pillow's textlength() REFUSES multiline text (ValueError), and a
        # newline reaches here easily: Qt's QLineEdit keeps '\n' on paste, and
        # an imported or hand-edited config can carry one. That raised through
        # every GUI render path into MainWindow.__init__, so once such a label
        # was saved the window could never open again. Fold newlines into
        # spaces and let _wrap lay the label out for the key.
        label = " ".join(label.split())
    if label:
        draw = ImageDraw.Draw(img)
        base_fs = max(10, int(size * (0.20 if drew_icon else 0.24)))
        max_w = size - 6
        # Shrink the font a little so a short label like "Bright +" stays on one
        # line (matching the other keys) before we fall back to wrapping.
        fs = base_fs
        min_fs = max(8, int(base_fs * 0.6))
        while fs > min_fs and draw.textlength(label, font=_font(fs)) > max_w:
            fs -= 1
        font = _font(fs)
        lines = _wrap(draw, label, font, max_w)
        line_h = fs + 2
        total_h = line_h * len(lines)
        y0 = (size - total_h) if drew_icon else (size - total_h) // 2
        y0 = max(0, min(y0, size - total_h))
        tcol = _hex(text_color, (255, 255, 255))
        for i, line in enumerate(lines):
            bb = draw.textbbox((0, 0), line, font=font)
            w = bb[2] - bb[0]
            x = int((size - w) // 2 - bb[0])
            y = y0 + i * line_h
            # subtle shadow for legibility over icons/backgrounds
            draw.text((x + 1, y + 1), line, font=font, fill=(0, 0, 0))
            draw.text((x, y), line, font=font, fill=tcol)

    if pressed:
        # Simple press feedback: flash the whole key brighter.
        img = ImageEnhance.Brightness(img.convert("RGB")).enhance(1.6)
    return img


def _wrap(draw, text, font, max_w):
    words = text.split()
    if not words:
        return [text]
    lines, cur = [], words[0]
    for w in words[1:]:
        trial = cur + " " + w
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines[:3]


def pil_to_qimage(image: Image.Image):
    """Convert a PIL image to a QImage (deep copy so it owns its buffer)."""
    from PyQt6.QtGui import QImage
    im = image.convert("RGBA")
    data = im.tobytes("raw", "RGBA")
    qimg = QImage(data, im.width, im.height, QImage.Format.Format_RGBA8888)
    return qimg.copy()


def to_device_jpeg(image: Image.Image, rotation: int = 0,
                   flip=(False, False), quality: int = 90) -> bytes:
    """Apply device orientation and return JPEG bytes ready for the transport."""
    if rotation:
        image = image.rotate(rotation)
    if flip[0]:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if flip[1]:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
