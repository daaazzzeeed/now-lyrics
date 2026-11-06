"""Палитра по обложке. Запускается коротким подпроцессом, чтобы Pillow не жил в памяти сервера.

    python -m lyrics_tv.palette_worker cover.jpg  →  {"bg": "#…", "card": "#…", "glow": "#…"}
"""
from __future__ import annotations

import colorsys
import json
import sys


def hx(h: float, l: float, s: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def dominant(path: str) -> tuple[float, float, float]:
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((64, 64))
        q = im.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette() or []
        colors = q.getcolors() or []
    scored = []
    for count, idx in colors:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        extreme = l < 0.08 or l > 0.93  # почти чёрный и почти белый — плохая основа
        scored.append((extreme, -count * (0.35 + s), (h, l, s)))
    if not scored:
        return 0.6, 0.3, 0.1
    scored.sort(key=lambda x: (x[0], x[1]))  # сначала нормальные цвета, затем по весу
    return scored[0][2]


VERSION = 2          # поднимать при изменении формулы — палитры в кэше пересчитаются
CARD_MAX_L = 0.42
MIN_CONTRAST = 4.0   # белый текст на карточке, WCAG


def luminance(hexcolor: str) -> float:
    def ch(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_white(hexcolor: str) -> float:
    return 1.05 / (luminance(hexcolor) + 0.05)


def card_color(h: float, s: float) -> str:
    """Карточка «Текст»: L ≤ 0.42, затем темнее шагами, пока белый не станет читаемым (≥ 4:1)."""
    sat = min(s * 0.85, 0.36)
    l = min(0.40, CARD_MAX_L)
    color = hx(h, l, sat)
    while contrast_white(color) < MIN_CONTRAST and l > 0.12:
        l -= 0.02
        color = hx(h, l, sat)
    return color


def palette(path: str) -> dict:
    h, _, s = dominant(path)
    return {
        "bg": hx(h, 0.14, min(s, 0.32)),
        "card": card_color(h, s),
        "glow": hx(h, 0.34, min(s, 0.55)),
        "v": VERSION,
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(palette(sys.argv[1])))
