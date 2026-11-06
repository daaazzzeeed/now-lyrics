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


def palette(path: str) -> dict:
    h, _, s = dominant(path)
    return {
        "bg": hx(h, 0.14, min(s, 0.32)),
        "card": hx(h, 0.40, min(s * 0.85, 0.36)),
        "glow": hx(h, 0.34, min(s, 0.55)),
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(palette(sys.argv[1])))
