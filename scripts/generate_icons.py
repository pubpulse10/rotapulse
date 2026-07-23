"""
One-off generator for RotaPulse's PWA app icons.

Unlike PricePulse/TaskPulse's icon scripts (which redraw a badge mark from
hardcoded SVG path coordinates), RotaPulse's source logo
(app/static/logo.svg, copied from pubpulse-hub/app/static/rotapulse-logo.svg)
is a wide wordmark+badge lockup wrapping a single embedded base64 PNG
(1988x568) rather than vector paths — there are no coordinates to redraw
from. This script instead decodes that embedded PNG directly and crops its
left square (the badge portion, since the image is exactly as tall as the
badge is wide) to build the icons from, via Pillow. If a future redesign
ships as real vector artwork instead, swap this script for the sibling
apps' coordinate-redraw approach.

Pillow is a one-off dev-time tool for this script only, not a runtime
dependency — not added to requirements.txt (same convention as the sibling
apps' own icon scripts).

Run once: python scripts/generate_icons.py
"""

import base64
import re
from pathlib import Path

from PIL import Image

SRC_SVG = Path(__file__).resolve().parent.parent / "app" / "static" / "logo.svg"
OUT_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
NAVY = (6, 34, 59, 255)


def _load_embedded_image() -> Image.Image:
    svg_text = SRC_SVG.read_text(encoding="utf-8")
    match = re.search(r'href="data:image/png;base64,([^"]+)"', svg_text)
    if not match:
        raise SystemExit(f"No embedded base64 PNG found in {SRC_SVG}")
    png_bytes = base64.b64decode(match.group(1))
    from io import BytesIO

    return Image.open(BytesIO(png_bytes)).convert("RGBA")


def _badge_square(img: Image.Image) -> Image.Image:
    """The badge is the leftmost square of the lockup — the image's own
    height is the badge's side length."""
    side = img.height
    return img.crop((0, 0, side, side))


def _on_navy_square(badge: Image.Image, size: int, *, full_bleed: bool) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if full_bleed:
        canvas.paste(Image.new("RGBA", (size, size), NAVY), (0, 0))
        inner = int(size * 0.66)
    else:
        inner = size
    resized = badge.resize((inner, inner), Image.LANCZOS)
    offset = (size - inner) // 2
    canvas.alpha_composite(resized, (offset, offset))
    return canvas


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    badge = _badge_square(_load_embedded_image())

    _on_navy_square(badge, 192, full_bleed=False).save(OUT_DIR / "icon-192.png")
    _on_navy_square(badge, 512, full_bleed=False).save(OUT_DIR / "icon-512.png")
    _on_navy_square(badge, 192, full_bleed=True).save(OUT_DIR / "icon-maskable-192.png")
    _on_navy_square(badge, 512, full_bleed=True).save(OUT_DIR / "icon-maskable-512.png")
    _on_navy_square(badge, 180, full_bleed=True).convert("RGB").save(OUT_DIR / "apple-touch-icon.png")

    # favicon.ico is NOT generated here any more — it's now the shared
    # PubPulse-family heartbeat mark, same across every app, sourced from
    # Businesses/PubPulse/Logos/PubPulse/generate_favicon.py. Re-run that
    # script and copy its output here if it ever needs regenerating.

    print(f"Wrote 5 icon files to {OUT_DIR}")


if __name__ == "__main__":
    main()
