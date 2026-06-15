"""Render the Scanlight additive-RGB app icon and build AppIcon.icns.

    python3 make_icon.py

Produces (next to this file): Scanlight.iconset/, AppIcon.icns, scanlight-logo.png.
Pure-PIL renderer — no SVG rasterizer needed. Circles are combined by
per-channel MAX (additive light), which yields clean yellow/cyan/magenta
overlaps and a true white centre, matching scanlight-logo.svg (mix-blend-mode:
lighten). The app bundle's CFBundleIconFile points at AppIcon.

Design (160-unit tile): dark squircle + three R/G/B circles.
"""
import os
import subprocess

from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
CIRCLES = [((80, 60), (255, 59, 63)),    # R (red=255)
           ((60, 95), (44, 255, 120)),   # G (green=255)
           ((100, 95), (54, 138, 255))]  # B (blue=255)
TILE = (19, 21, 27, 255)
RADIUS = 36.0


def render(S, margin_frac=0.098):
    """SxS RGBA icon. margin_frac 0.098 = macOS icon grid; 0.0 = full-bleed logo."""
    m = S * margin_frac
    x0, y0, x1, y1 = m, m, S - m, S - m
    C = x1 - x0
    rad = C * 0.2247
    f = C / 160.0

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=TILE)
    squircle = Image.new("L", (S, S), 0)
    ImageDraw.Draw(squircle).rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=255)

    add = Image.new("RGB", (S, S), (0, 0, 0))
    union = Image.new("L", (S, S), 0)
    for (cx, cy), col in CIRCLES:
        box = [x0 + (cx - RADIUS) * f, y0 + (cy - RADIUS) * f,
               x0 + (cx + RADIUS) * f, y0 + (cy + RADIUS) * f]
        layer = Image.new("RGB", (S, S), (0, 0, 0))
        ImageDraw.Draw(layer).ellipse(box, fill=col)
        add = ImageChops.lighter(add, layer)
        ImageDraw.Draw(union).ellipse(box, fill=255)
    union = ImageChops.darker(union, squircle)

    add_rgba = add.convert("RGBA")
    add_rgba.putalpha(union)
    return Image.alpha_composite(img, add_rgba)


def main():
    master = render(2048, margin_frac=0.098)
    iconset = os.path.join(HERE, "Scanlight.iconset")
    os.makedirs(iconset, exist_ok=True)
    sizes = {
        "icon_16x16.png": 16, "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128, "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256, "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
    }
    for name, px in sizes.items():
        master.resize((px, px), Image.LANCZOS).save(os.path.join(iconset, name))
    render(1024, margin_frac=0.0).save(os.path.join(HERE, "scanlight-logo.png"))
    subprocess.run(
        ["iconutil", "-c", "icns", iconset, "-o", os.path.join(HERE, "AppIcon.icns")],
        check=True,
    )
    print("wrote AppIcon.icns, Scanlight.iconset/, scanlight-logo.png")


if __name__ == "__main__":
    main()
