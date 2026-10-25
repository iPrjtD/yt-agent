"""Buzzbee brand assets drawn in code (reproducible, reusable for thumbnails and intros).

`uv run ytagent brand render` writes everything to brand/.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ytagent.config import ROOT

HONEY = (255, 195, 0)
CHARCOAL = (20, 20, 20)
INK = (26, 26, 26)
CREAM = (255, 248, 231)
CORAL = (255, 90, 95)
WING = (189, 232, 255)
GREY = (176, 176, 176)
HEX_LINE = (34, 34, 34)

FONTS = ROOT / "assets" / "fonts"
OUT = ROOT / "brand"
SS = 4  # supersampling factor for smooth edges


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"Poppins-{weight}.ttf"), size)


def _bezier(p0, p1, p2, steps: int = 40) -> list[tuple[float, float]]:
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t**2 * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t**2 * p2[1])
            for t in (i / steps for i in range(steps + 1))]


EXPRESSIONS = ("neutral", "shocked", "laugh", "cool", "think", "sad", "love")


def mascot(size: int = 1024, expression: str = "neutral", mouth: float = 0.0) -> Image.Image:
    """The Buzzbee reporter (headphones + mic) on a transparent square canvas.

    `expression` is one of EXPRESSIONS; `mouth` (0..1) opens the mouth for talking animation.
    Coordinates are in a 1000x1000 design space, scaled to the output size.
    """
    w = size * SS
    k = w / 1000

    def pt(x, y):
        return (x * k, y * k)

    def box(cx, cy, bw, bh):
        return [(cx - bw / 2) * k, (cy - bh / 2) * k, (cx + bw / 2) * k, (cy + bh / 2) * k]

    def px(v):
        return max(1, round(v * k))

    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))

    # Wings: own layers so they can be rotated and stay translucent.
    for cx, angle in ((265, -28), (735, 28)):
        layer = Image.new("RGBA", (w, w), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.ellipse(box(cx, 300, 290, 175), fill=WING + (225,), outline=(255, 255, 255, 255), width=px(10))
        d.line([pt(cx - 105, 300), pt(cx + 105, 300)], fill=(255, 255, 255, 200), width=px(6))
        img.alpha_composite(layer.rotate(angle, center=pt(cx, 300), resample=Image.BICUBIC))

    d = ImageDraw.Draw(img)

    # Body with stripes clipped to the body shape.
    body_box = box(500, 795, 470, 330)
    body = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    bd.ellipse(body_box, fill=HONEY + (255,))
    for y0, y1 in ((728, 780), (845, 897)):
        bd.rectangle([0, y0 * k, w, y1 * k], fill=INK + (255,))
    mask = Image.new("L", (w, w), 0)
    ImageDraw.Draw(mask).ellipse(body_box, fill=255)
    img.paste(body, (0, 0), mask)
    d.ellipse(body_box, outline=INK, width=px(12))

    # Head.
    d.ellipse(box(500, 450, 510, 510), fill=HONEY, outline=INK, width=px(12))

    # Antennae (drawn before the headphone band so the band sits over their roots).
    for root, ctrl, tip in (((420, 215), (395, 110), (318, 78)), ((580, 215), (605, 110), (682, 78))):
        d.line([pt(*p) for p in _bezier(root, ctrl, tip)], fill=INK, width=px(16), joint="curve")
        d.ellipse(box(tip[0], tip[1], 62, 62), fill=INK)
        d.ellipse(box(tip[0] - 10, tip[1] - 10, 18, 18), fill=(90, 90, 90))

    # Headphones: band over the head, cups at the sides.
    d.arc([210 * k, 160 * k, 790 * k, 740 * k], start=198, end=342, fill=INK, width=px(38))
    for x0 in (188, 742):
        d.rounded_rectangle([x0 * k, 372 * k, (x0 + 70) * k, 548 * k], radius=px(30), fill=INK)
        d.rounded_rectangle([(x0 + 16) * k, 400 * k, (x0 + 54) * k, 520 * k], radius=px(18), fill=HONEY)

    # Mic boom from the left cup to the mouth, with a red "on air" dot.
    d.line([pt(*p) for p in _bezier((228, 540), (250, 655), (372, 640))], fill=INK, width=px(14), joint="curve")
    d.ellipse(box(392, 638, 56, 56), fill=INK)
    d.ellipse(box(392, 638, 18, 18), fill=CORAL)

    _face(img, expression, mouth, k)
    return img.resize((size, size), Image.LANCZOS)


TEAR = (120, 200, 255)
HEART = (235, 45, 70)


def _face(img: Image.Image, expression: str, mouth: float, k: float) -> None:
    """Eyes, brows, cheeks and mouth for each expression (design space 1000x1000)."""
    d = ImageDraw.Draw(img)

    def box(cx, cy, bw, bh):
        return [(cx - bw / 2) * k, (cy - bh / 2) * k, (cx + bw / 2) * k, (cy + bh / 2) * k]

    def px(v):
        return max(1, round(v * k))

    def line(*pts, width=12):
        d.line([(x * k, y * k) for x, y in pts], fill=INK, width=px(width), joint="curve")

    def round_eyes(w_=120, h_=142, pupil=88, dx=8, dy=12):
        for ex in (410, 590):
            d.ellipse(box(ex, 440, w_, h_), fill=(255, 255, 255), outline=INK, width=px(6))
            d.ellipse(box(ex + dx, 440 + dy, pupil, pupil), fill=INK)
            d.ellipse(box(ex + dx + 16, 440 + dy - 24, pupil * 0.34, pupil * 0.34), fill=(255, 255, 255))

    def heart(cx, cy, s):
        import math as _m
        pts = [(cx + s * 16 * _m.sin(t) ** 3 / 17, cy - s * (13 * _m.cos(t) - 5 * _m.cos(2 * t)
                - 2 * _m.cos(3 * t) - _m.cos(4 * t)) / 17) for t in [i / 40 * 2 * _m.pi for i in range(41)]]
        d.polygon([(x * k, y * k) for x, y in pts], fill=HEART, outline=INK)

    if expression == "shocked":
        round_eyes(w_=140, h_=172, pupil=46, dx=0, dy=0)
        line((350, 340), (410, 318), (465, 338))
        line((535, 338), (590, 318), (650, 340))
    elif expression == "laugh":
        for ex in (410, 590):
            d.arc(box(ex, 462, 104, 84), start=200, end=340, fill=INK, width=px(16))
        d.ellipse(box(338, 500, 26, 40), fill=TEAR)  # tear of joy
    elif expression == "cool":
        for ex in (410, 590):
            d.rounded_rectangle(box(ex, 445, 156, 104), radius=px(30), fill=INK)
            line((ex - 50, 420), (ex - 20, 400), width=8)
            d.line([((ex - 50) * k, 420 * k), ((ex - 20) * k, 400 * k)], fill=(200, 200, 200), width=px(8))
        line((488, 438), (512, 438), width=14)
    elif expression == "think":
        round_eyes(dx=22, dy=-18)
        line((540, 352), (600, 330), (655, 342))
    elif expression == "sad":
        round_eyes(dx=0, dy=26)
        line((355, 372), (455, 348))  # inner ends raised = sad (the reverse reads as angry)
        line((545, 348), (645, 372))
        d.ellipse(box(392, 535, 26, 42), fill=TEAR)
    elif expression == "love":
        heart(410, 445, 70)
        heart(590, 445, 70)
    elif expression == "blink":
        for ex in (410, 590):
            d.arc(box(ex, 430, 110, 70), start=20, end=160, fill=INK, width=px(12))
    else:
        round_eyes()

    # Cheeks (blended layer).
    cheeks = Image.new("RGBA", img.size, (0, 0, 0, 0))
    cd = ImageDraw.Draw(cheeks)
    for cx in (338, 662):
        cd.ellipse(box(cx, 548, 84, 48), fill=(255, 120, 135, 165))
    img.alpha_composite(cheeks)
    d = ImageDraw.Draw(img)

    # Mouth.
    if expression == "shocked":
        d.ellipse(box(505, 585, 74, 96), fill=INK)
    elif expression == "laugh":
        d.chord(box(505, 540, 180, 150), start=0, end=180, fill=INK)
        d.chord(box(505, 588, 92, 50), start=0, end=180, fill=(255, 110, 130))
    elif expression == "sad":
        d.arc(box(505, 612, 120, 80), start=200, end=340, fill=INK, width=px(12))
    elif expression == "think":
        d.line([(470 * k, 590 * k), (545 * k, 578 * k)], fill=INK, width=px(12))
    elif expression == "cool":
        d.arc(box(530, 548, 110, 80), start=10, end=120, fill=INK, width=px(12))
    elif mouth > 0.05:  # talking
        h = 20 + 80 * min(1.0, mouth)
        d.ellipse(box(505, 570 + h / 4, 104, h), fill=INK)
        if mouth > 0.4:
            d.ellipse(box(505, 570 + h / 2.6, 60, h / 2.8), fill=(255, 110, 130))
    else:
        d.arc(box(505, 545, 120, 100), start=20, end=160, fill=INK, width=px(12))


def radial_glow(size: tuple[int, int], center: tuple[int, int], radius: int, color, alpha: int) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    cx, cy = center
    ImageDraw.Draw(layer).ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=color + (alpha,))
    return layer.filter(ImageFilter.GaussianBlur(radius / 2.2))


def honeycomb(img: Image.Image, r: int, color, width: int) -> None:
    d = ImageDraw.Draw(img)
    hw, hh = math.sqrt(3) * r, 1.5 * r
    rows, cols = int(img.height / hh) + 2, int(img.width / hw) + 2
    for row in range(rows):
        for col in range(cols):
            cx = col * hw + (hw / 2 if row % 2 else 0)
            cy = row * hh
            pts = [(cx + r * math.cos(math.radians(60 * i - 30)), cy + r * math.sin(math.radians(60 * i - 30)))
                   for i in range(6)]
            d.polygon(pts, outline=color, width=width)


def profile_picture(size: int = 800) -> Image.Image:
    img = Image.new("RGBA", (size, size), CHARCOAL + (255,))
    img.alpha_composite(radial_glow(img.size, (size // 2, size // 2), int(size * 0.42), HONEY, 70))
    d = ImageDraw.Draw(img)
    inset = int(size * 0.035)
    d.ellipse([inset, inset, size - inset, size - inset], outline=HONEY, width=max(2, size // 90))
    bee = mascot(int(size * 0.86))
    img.alpha_composite(bee, ((size - bee.width) // 2, int(size * 0.10)))
    # Keep only the circle YouTube shows; the corners stay charcoal for platforms that use squares.
    return img


def watermark(size: int = 150) -> Image.Image:
    pic = profile_picture(size * SS)
    mask = Image.new("L", pic.size, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, pic.width - 1, pic.height - 1], fill=255)
    pic.putalpha(mask)
    return pic.resize((size, size), Image.LANCZOS)


def banner(channel_name: str, tagline: str, strapline: str) -> Image.Image:
    """2560x1440 banner; all content inside YouTube's 1546x423 safe area (visible on every device)."""
    w, h = 2560, 1440
    img = Image.new("RGBA", (w, h), CHARCOAL + (255,))
    honeycomb(img, 64, HEX_LINE, 3)

    f_name, f_tag, f_strap = font("Black", 178), font("SemiBold", 66), font("Medium", 42)
    text_w = max(f.getbbox(t)[2] for f, t in ((f_name, channel_name), (f_tag, tagline), (f_strap, strapline)))
    bee_size, gap = 450, 50
    group_w = bee_size + gap + text_w
    x0 = (w - group_w) // 2
    cy = h // 2

    img.alpha_composite(radial_glow(img.size, (x0 + bee_size // 2, cy), 330, HONEY, 85))
    bee = mascot(bee_size)
    img.alpha_composite(bee, (x0, cy - bee_size // 2))

    d = ImageDraw.Draw(img)
    tx = x0 + bee_size + gap
    name_h = f_name.getbbox(channel_name)[3]
    top = cy - (name_h + 24 + 70 + 34 + 46) // 2 - 18
    d.text((tx, top), channel_name, font=f_name, fill=HONEY)
    d.text((tx + 6, top + name_h + 24), tagline, font=f_tag, fill=CREAM)
    d.text((tx + 8, top + name_h + 24 + 70 + 34), strapline, font=f_strap, fill=GREY)
    return img


def render_all(channel_name: str, tagline: str) -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "mascot.png": mascot(1024),
        "profile_picture.png": profile_picture(800).convert("RGB"),
        "watermark.png": watermark(150),
        "banner.png": banner(channel_name, tagline, "New Shorts daily  â€¢  Deep dives weekly").convert("RGB"),
    }
    paths = []
    for name, image in files.items():
        path = OUT / name
        image.save(path, optimize=True)
        paths.append(path)
    return paths
