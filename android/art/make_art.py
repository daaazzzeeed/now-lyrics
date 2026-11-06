"""Иконка и баннер Lyrics TV: яркий градиент + облачко речи с вырезанными нотами.

Пересобрать (нужен Pillow, например из venv бэкенда):
    backend/.venv/bin/python android/art/make_art.py android/app/src/main/res backend/web/fonts /tmp
"""import math
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

RES, FONTS, PREV = sys.argv[1], sys.argv[2], sys.argv[3]
SS = 4

STOPS = [(0.0, (104, 58, 255)), (0.52, (255, 58, 128)), (1.0, (255, 150, 72))]


def grad_color(k):
    for (k0, c0), (k1, c1) in zip(STOPS, STOPS[1:]):
        if k <= k1:
            t = (k - k0) / (k1 - k0)
            t = t * t * (3 - 2 * t)
            return tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))
    return STOPS[-1][1]


def background(w, h):
    """Диагональный трёхцветный градиент + лёгкий блик в левом верхнем углу."""
    gw, gh = 256, max(2, round(256 * h / w))
    g = Image.new("RGB", (gw, gh))
    px = g.load()
    for y in range(gh):
        for x in range(gw):
            k = (x / gw * w + y / gh * h) / (w + h)
            px[x, y] = grad_color(min(1, max(0, k)))
    im = g.resize((w, h), Image.BICUBIC).convert("RGBA")
    hl = Image.new("L", (w, h), 0)
    r = max(w, h)
    ImageDraw.Draw(hl).ellipse([-r * .45, -r * .55, r * .55, r * .35], fill=60)
    hl = hl.filter(ImageFilter.GaussianBlur(r * .2))
    white = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    white.putalpha(hl)
    im.alpha_composite(white)
    return im


def ellipse_rot(size, cx, cy, rx, ry, angle):
    layer = Image.new("L", size, 0)
    ImageDraw.Draw(layer).ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
    return layer.rotate(angle, center=(cx, cy), resample=Image.BICUBIC)


def notes_mask(S):
    """Две ноты с ребром (♫) в квадрате S×S."""
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    rx, ry, ang = S * .165, S * .118, 22
    heads = [(S * .25, S * .80), (S * .75, S * .70)]
    stem_w = S * .085
    beam_h = S * .17
    tops = [S * .12, S * .02]
    for (cx, cy) in heads:
        m = ImageChops.lighter(m, ellipse_rot((S, S), cx, cy, rx, ry, ang))
    d = ImageDraw.Draw(m)
    xs = []
    for (cx, cy), top in zip(heads, tops):
        x_right = cx + rx * .93
        d.rectangle([x_right - stem_w, top + beam_h * .5, x_right, cy - ry * .15], fill=255)
        xs.append(x_right)
    (x1, x2), (t1, t2) = xs, tops
    d.polygon([(x1 - stem_w, t1), (x2, t2), (x2, t2 + beam_h), (x1 - stem_w, t1 + beam_h)], fill=255)
    return m


def smooth(mask, r):
    """Скругляет углы: размытие + порог."""
    return mask.filter(ImageFilter.GaussianBlur(r)).point(lambda v: 255 if v > 127 else 0)


def bubble_mask(N):
    """Облачко речи с хвостиком; ноты вырезаны. Возвращает маску N×N."""
    m = Image.new("L", (N, N), 0)
    d = ImageDraw.Draw(m)
    x0, y0, x1, y1 = N * .14, N * .17, N * .86, N * .74
    d.rounded_rectangle([x0, y0, x1, y1], radius=N * .17, fill=255)
    d.polygon([(N * .26, y1 - N * .05), (N * .46, y1 - N * .02), (N * .22, N * .89)], fill=255)
    m = smooth(m, N * .012)
    S = int(N * .36)
    nm = notes_mask(S)
    ox = int((x0 + x1) / 2 - S / 2 + N * .005)
    oy = int((y0 + y1) / 2 - S / 2 + N * .01)
    hole = Image.new("L", (N, N), 0)
    hole.paste(nm, (ox, oy))
    return ImageChops.subtract(m, hole)


def glyph_layer(N):
    """Белое облачко + мягкая тень под ним, на прозрачном фоне."""
    mask = bubble_mask(N)
    out = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    sh = Image.new("RGBA", (N, N), (60, 10, 60, 0))
    sh_mask = Image.new("L", (N, N), 0)
    sh_mask.paste(mask.point(lambda v: v * 0.32), (0, int(N * .025)))
    sh.putalpha(sh_mask.filter(ImageFilter.GaussianBlur(N * .03)))
    out.alpha_composite(sh)
    white = Image.new("RGBA", (N, N), (255, 255, 255, 0))
    white.putalpha(mask)
    out.alpha_composite(white)
    return out


def rounded(im, radius):
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.size[0] - 1, im.size[1] - 1], radius=radius, fill=255)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def legacy_icon(n):
    N = n * SS
    im = background(N, N)
    g = glyph_layer(int(N * .86))
    off = (N - g.size[0]) // 2
    im.alpha_composite(g, (off, off))
    return rounded(im, N * .23).resize((n, n), Image.LANCZOS)


def adaptive(n):
    """108dp: фон и передний план. Символ — в безопасной зоне (66dp)."""
    N = n * SS
    bg = background(N, N).convert("RGB").resize((n, n), Image.LANCZOS)
    inner = int(N * 74 / 108)
    fg = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    fg.alpha_composite(glyph_layer(inner), ((N - inner) // 2, (N - inner) // 2))
    return bg, fg.resize((n, n), Image.LANCZOS)


def banner(w, h):
    W, H = w * SS, h * SS
    im = background(W, H)
    gs = int(H * .64)
    size = int(H * .20)
    fb = ImageFont.truetype(f"{FONTS}/InterDisplay-ExtraBold.woff", size)
    fl = ImageFont.truetype(f"{FONTS}/InterDisplay-SemiBold.woff", size)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    gap_g = int(gs * -.02)          # у облачка есть поля, поэтому текст можно чуть придвинуть
    gap_t = int(H * .045)
    w_lyr = probe.textlength("Lyrics", font=fb)
    w_tv = probe.textlength("TV", font=fl)
    total = gs + gap_g + w_lyr + gap_t + w_tv
    gx = int((W - total) / 2)
    im.alpha_composite(glyph_layer(gs), (gx, (H - gs) // 2))
    x = gx + gs + gap_g
    bb = probe.textbbox((0, 0), "Lyrics", font=fb)
    y = (H - (bb[3] - bb[1])) // 2 - bb[1] - int(H * .025)
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).text((x, y + int(H * .012)), "Lyrics", font=fb, fill=(70, 10, 60, 70))
    im.alpha_composite(sh.filter(ImageFilter.GaussianBlur(H * .02)))
    txt = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(txt)
    d.text((x, y), "Lyrics", font=fb, fill=(255, 255, 255, 255))
    d.text((x + w_lyr + gap_t, y), "TV", font=fl, fill=(255, 255, 255, 190))
    im.alpha_composite(txt)
    return im.convert("RGB").resize((w, h), Image.LANCZOS)


banner(320, 180).save(f"{RES}/drawable/banner.png")
b = banner(640, 360)
b.save(f"{RES}/drawable-xhdpi/banner.png")
b.save(f"{PREV}/banner2.png")
for dens, n in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192)):
    legacy_icon(n).save(f"{RES}/mipmap-{dens}/ic_launcher.png")
    bgl, fgl = adaptive(round(n * 108 / 48))
    bgl.save(f"{RES}/mipmap-{dens}/ic_launcher_background.png")
    fgl.save(f"{RES}/mipmap-{dens}/ic_launcher_foreground.png")

# превью: крупно, круглая маска и мелкие размеры на тёмном фоне
sheet = Image.new("RGBA", (1000, 520), (24, 24, 28, 255))
sheet.alpha_composite(legacy_icon(420), (30, 50))
bgl, fgl = adaptive(432)
c = bgl.convert("RGBA"); c.alpha_composite(fgl)
cm = Image.new("L", c.size, 0); ImageDraw.Draw(cm).ellipse([20, 20, 411, 411], fill=255)
cc = Image.new("RGBA", c.size, (0, 0, 0, 0)); cc.paste(c, (0, 0), cm)
sheet.alpha_composite(cc.resize((300, 300), Image.LANCZOS), (480, 30))
for i, n in enumerate((96, 72, 48)):
    sheet.alpha_composite(legacy_icon(n), (480 + i * 130, 380))
sheet.save(f"{PREV}/icon2.png")
