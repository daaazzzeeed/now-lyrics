"""Жирный CJK-шрифт под конкретный трек: подмножество Noto Sans JP/KR/SC/TC Bold (OFL).

На ТВ есть только regular Noto Sans CJK, а синтетический жирный выглядит плохо. Поэтому бэкенд
собирает woff2 только из символов текста и названия трека (обычно 20–60 КБ). Полный шрифт лежит
на диске; fontTools работает в подпроцессе (font_worker), чтобы не держать его в памяти сервера.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path

import httpx

from . import config as C
from .lrc import is_traditional

log = logging.getLogger("fonts")

KEEP_SUBSETS = 300
_locks: dict[str, asyncio.Lock] = {}


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3000 <= o <= 0x30FF or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF
            or 0xFF00 <= o <= 0xFFEF or 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F
            or 0x31F0 <= o <= 0x31FF)


def _texts(track: dict, raw: dict | None) -> list[str]:
    out = [track.get("title", ""), *track.get("artists", []), track.get("album", "")]
    for ln in (raw or {}).get("lines", []):
        out += [ln.get("text", ""), ln.get("sub", "")]
    return [t for t in out if t]


def family(lang: str | None, text: str) -> str | None:
    if lang == "ja":
        return "JP"
    if lang == "ko":
        return "KR"
    if lang == "yue":
        return "TC"
    if lang == "zh":
        return "TC" if is_traditional(text) else "SC"
    return None


def _is_hangul(ch: str) -> bool:
    o = ord(ch)
    return 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F


RANGES = {
    "hangul": "U+AC00-D7AF, U+1100-11FF, U+3130-318F",
    "cjk": "U+3000-30FF, U+3400-4DBF, U+4E00-9FFF, U+F900-FAFF, U+FF00-FFEF, U+31F0-31FF",
}
RANGES["all"] = RANGES["cjk"] + ", " + RANGES["hangul"]


def plan(track: dict, raw: dict | None, lang: str | None) -> list[tuple[str, str, str, str]]:
    """→ [(семейство, символы, хеш, диапазон)]. В китайском и японском подмножествах Noto нет хангыля,
    поэтому для смешанных треков (китайский + корейский) хангыль идёт отдельным KR-шрифтом."""
    texts = _texts(track, raw)
    chars = sorted({ch for t in texts for ch in t if _is_cjk(ch)})
    if not chars:
        return []
    joined = "".join(texts)
    main = family(lang, joined) or ("KR" if any(_is_hangul(c) for c in chars) else family("zh", joined))
    groups: dict[tuple[str, str], list[str]] = {}
    for ch in chars:
        key = (main, "all") if main == "KR" else (("KR", "hangul") if _is_hangul(ch) else (main, "cjk"))
        groups.setdefault(key, []).append(ch)
    out = []
    for (fam, rng), cs in groups.items():
        text = "".join(cs)
        out.append((fam, text, hashlib.sha1(f"{fam}|{text}".encode()).hexdigest()[:12], rng))
    return out


def font_urls(track: dict, raw: dict | None, lang: str | None) -> list[dict]:
    return [{"url": f"/font/{track['id']}-{fam}.woff2?h={h}", "range": RANGES[rng]}
            for fam, _, h, rng in plan(track, raw, lang)]


def _src_path(fam: str) -> Path:
    return C.FONTS_DIR / "src" / f"NotoSans{fam}-Bold.otf"


async def _ensure_source(client: httpx.AsyncClient, fam: str) -> Path:
    path = _src_path(fam)
    if path.exists():
        return path
    async with _locks.setdefault(f"src:{fam}", asyncio.Lock()):
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{C.FONT_BASE}/{fam}/NotoSans{fam}-Bold.otf"
        log.info("качаю шрифт %s", url)
        tmp = path.with_suffix(".part")
        async with client.stream("GET", url, timeout=httpx.Timeout(60, connect=10)) as r:
            if r.status_code != 200:
                raise RuntimeError(f"шрифт {fam}: HTTP {r.status_code}")
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
        tmp.replace(path)
        return path


def _prune() -> None:
    files = sorted(C.FONTS_DIR.glob("*.woff2"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_SUBSETS:]:
        old.unlink(missing_ok=True)


async def ensure_subset(client: httpx.AsyncClient, track: dict, raw: dict | None, lang: str | None,
                        want: str) -> Path | None:
    p = next((x for x in plan(track, raw, lang) if x[0] == want), None)
    if not p:
        return None
    fam, chars, h, _ = p
    out = C.FONTS_DIR / f"{track['id']}-{h}.woff2"
    if out.exists():
        os.utime(out)
        return out
    async with _locks.setdefault(f"sub:{out.name}", asyncio.Lock()):
        if out.exists():
            return out
        src = await _ensure_source(client, fam)
        chars_file = out.with_suffix(".txt")
        chars_file.write_text(chars, encoding="utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "lyrics_tv.font_worker", str(src), str(out), str(chars_file),
                cwd=str(C.BASE), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        finally:
            chars_file.unlink(missing_ok=True)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f"подмножество шрифта: {err.decode(errors='replace').strip()[-300:]}")
        _prune()
        log.info("шрифт %s для %s: %d символов, %d КБ", fam, track["id"], len(chars), out.stat().st_size // 1024)
        return out
