"""Иконка и баннер Lyrics TV: зелёный фон, чёрная жирная «Ly» и полоска прогресса.

Пересобрать (нужен Pillow, например из venv бэкенда):
    backend/.venv/bin/python android/art/make_art.py android/app/src/main/res backend/web/fonts [PREVIEW_DIR]
"""
import sys

from PIL import Image, ImageDraw, ImageFont

RES, FONTS = sys.argv[1], sys.argv[2]
PREV = sys.argv[3] if len(sys.argv) > 3 else None
SS = 4  # суперсэмплинг для гладких краёв

GREEN = (30, 215, 96, 255)
INK = (18, 18, 18)


def font(weight: str, size: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(f"{FONTS}/InterDisplay-{weight}.woff", int(size))


def progress(d: ImageDraw.ImageDraw, x: float, y: float, w: float, h: float, k: float, knob: float) -> None:
    """Полоска прогресса: дорожка, заполненная часть и «бегунок»."""
    d.rounded_rectangle([x, y, x + w, y + h], radius=h / 2, fill=INK + (70,))
    d.rounded_rectangle([x, y, x + w * k, y + h], radius=h / 2, fill=INK + (255,))
    cx, cy = x + w * k, y + h / 2
    d.ellipse([cx - knob, cy - knob, cx + knob, cy + knob], fill=INK + (255,))


def mark(N: int) -> Image.Image:
    """«Ly» + полоска на прозрачном фоне, квадрат N×N."""
    layer = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = font("ExtraBold", N * .52)
    bb = d.textbbox((0, 0), "Ly", font=f)
    d.text((N * .5 - (bb[0] + bb[2]) / 2, N * .44 - (bb[1] + bb[3]) / 2), "Ly", font=f, fill=INK + (255,))
    progress(d, N * .24, N * .78, N * .52, N * .05, .55, N * .045)
    return layer


def rounded(im: Image.Image, r: float) -> Image.Image:
    m = Image.new("L", im.size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, im.size[0] - 1, im.size[1] - 1], radius=r, fill=255)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im, (0, 0), m)
    return out


def legacy_icon(n: int) -> Image.Image:
    N = n * SS
    im = Image.new("RGBA", (N, N), GREEN)
    im.alpha_composite(mark(N))
    return rounded(im, N * .22).resize((n, n), Image.LANCZOS)


def adaptive(n: int) -> tuple[Image.Image, Image.Image]:
    """108dp: сплошной зелёный фон и передний план; знак — в безопасной зоне (~66dp)."""
    N = n * SS
    bg = Image.new("RGB", (n, n), GREEN[:3])
    inner = int(N * 70 / 108)
    fg = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    fg.alpha_composite(mark(inner), ((N - inner) // 2, (N - inner) // 2))
    return bg, fg.resize((n, n), Image.LANCZOS)


def banner(w: int, h: int) -> Image.Image:
    W, H = w * SS, h * SS
    im = Image.new("RGBA", (W, H), GREEN)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = font("ExtraBold", H * .27)
    tw = d.textlength("Lyrics TV", font=f)
    x = (W - tw) / 2
    bb = d.textbbox((0, 0), "Lyrics", font=f)
    y = H * .44 - (bb[1] + bb[3]) / 2
    d.text((x, y), "Lyrics", font=f, fill=INK + (255,))
    d.text((x + d.textlength("Lyrics ", font=f), y), "TV", font=f, fill=INK + (120,))
    progress(d, x, H * .7, tw, H * .028, .42, H * .028)
    im.alpha_composite(layer)
    return im.convert("RGB").resize((w, h), Image.LANCZOS)


banner(320, 180).save(f"{RES}/drawable/banner.png")
banner(640, 360).save(f"{RES}/drawable-xhdpi/banner.png")
for dens, n in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192)):
    legacy_icon(n).save(f"{RES}/mipmap-{dens}/ic_launcher.png")
    bgl, fgl = adaptive(round(n * 108 / 48))
    bgl.save(f"{RES}/mipmap-{dens}/ic_launcher_background.png")
    fgl.save(f"{RES}/mipmap-{dens}/ic_launcher_foreground.png")

if PREV:  # лист для просмотра: иконка, круглая адаптивная, мелкие размеры, баннер
    sheet = Image.new("RGBA", (1180, 420), (40, 40, 44, 255))
    sheet.alpha_composite(legacy_icon(300), (30, 30))
    bgl, fgl = adaptive(432)
    c = bgl.convert("RGBA")
    c.alpha_composite(fgl)
    cm = Image.new("L", c.size, 0)
    ImageDraw.Draw(cm).ellipse([20, 20, 411, 411], fill=255)
    cc = Image.new("RGBA", c.size, (0, 0, 0, 0))
    cc.paste(c, (0, 0), cm)
    sheet.alpha_composite(cc.resize((200, 200), Image.LANCZOS), (350, 30))
    sheet.alpha_composite(legacy_icon(64), (350, 260))
    sheet.alpha_composite(legacy_icon(48), (430, 270))
    sheet.alpha_composite(banner(640, 360).convert("RGBA").resize((560, 315), Image.LANCZOS), (590, 30))
    sheet.convert("RGB").save(f"{PREV}/final.png")
