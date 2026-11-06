"""DEMO=1: фейковый плеер без Spotify — японский трек, английский, инструментал и простой по кругу."""
from __future__ import annotations

import json
import time

from . import config as C
from .lrc import attach, lyrics_from_lrc, parse_lrc


class DemoPlayer:
    def __init__(self):
        data = json.loads((C.DEMO_DIR / "demo.json").read_text(encoding="utf-8"))
        self.tracks = data["tracks"]
        self.idle_ms = int(data.get("idle_ms", 15000))
        self.t0 = time.monotonic() - C.DEMO_START_MS / 1000

    async def player(self) -> dict | None:
        cycle = sum(t["duration_ms"] for t in self.tracks) + self.idle_ms
        el = int((time.monotonic() - self.t0) * 1000) % cycle
        for t in self.tracks:
            if el < t["duration_ms"]:
                return {
                    "is_playing": True, "progress_ms": el,
                    "item": {"type": "track", "id": t["id"], "name": t["title"], "duration_ms": t["duration_ms"],
                             "artists": [{"name": a} for a in t["artists"]],
                             "album": {"name": t["album"], "images": [{"url": "demo:" + t["cover"], "width": 640}]}},
                }
            el -= t["duration_ms"]
        return None

    def lyrics(self, tid: str) -> dict | None:
        t = next((x for x in self.tracks if x["id"] == tid), None)
        if not t or not t.get("lrc"):
            return None
        lyr = lyrics_from_lrc(t["lrc"], "", "demo")
        if lyr and t.get("romalrc"):
            attach(lyr["lines"], parse_lrc(t["romalrc"])[0], "romaji")
        return lyr
