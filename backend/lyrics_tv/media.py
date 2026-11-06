"""Обложки на диске ВМ (ТВ не ходит на CDN Spotify) и палитра через подпроцесс."""
from __future__ import annotations

import asyncio
import colorsys
import hashlib
import json
import logging
import os
import shutil
import sys
from pathlib import Path

import httpx

from . import config as C

log = logging.getLogger("media")

DEFAULT_PALETTE = {"bg": "#22262d", "card": "#5a6a7d", "glow": "#3b4b5e"}
_locks: dict[str, asyncio.Lock] = {}


def _hex(h: float, l: float, s: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def fallback_palette(tid: str) -> dict:
    """Пока считается настоящая палитра (или если обложки нет) — оттенок по хешу трека."""
    h = int(hashlib.md5(tid.encode()).hexdigest()[:4], 16) / 65535
    return {"bg": _hex(h, 0.14, 0.22), "card": _hex(h, 0.40, 0.28), "glow": _hex(h, 0.32, 0.40)}


def cover_path(tid: str) -> Path:
    return C.COVERS_DIR / f"{tid}.jpg"


def _prune() -> None:
    files = sorted(C.COVERS_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[C.COVERS_KEEP:]:
        old.unlink(missing_ok=True)


async def ensure_cover(client: httpx.AsyncClient, info: dict) -> Path | None:
    path = cover_path(info["id"])
    lock = _locks.setdefault(info["id"], asyncio.Lock())
    async with lock:
        try:
            if path.exists():
                os.utime(path)
                return path
            urls = info.get("images") or []
            if not urls:
                return None
            C.COVERS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            url = urls[0]
            if url.startswith("demo:"):
                shutil.copyfile(C.DEMO_DIR / Path(url[5:]).name, tmp)
            else:
                r = await client.get(url, timeout=15)
                if r.status_code != 200 or not r.content:
                    log.info("обложка %s: HTTP %s", info["id"], r.status_code)
                    return None
                tmp.write_bytes(r.content)
            tmp.replace(path)
            _prune()
            return path
        finally:
            _locks.pop(info["id"], None)


async def compute_palette(path: Path) -> dict | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "lyrics_tv.palette_worker", str(path),
            cwd=str(C.BASE), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (OSError, asyncio.TimeoutError) as e:
        log.warning("палитра: %s", e)
        return None
    if proc.returncode != 0:
        log.warning("палитра: %s", err.decode(errors="replace").strip()[-300:])
        return None
    try:
        pal = json.loads(out)
    except ValueError:
        return None
    return pal if isinstance(pal, dict) and {"bg", "card", "glow"} <= pal.keys() else None
