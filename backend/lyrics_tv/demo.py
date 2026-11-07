"""DEMO=1: фейковый плеер без Spotify — японский трек, английский, инструментал и простой по кругу."""
from __future__ import annotations

import json
import random
import time

from . import config as C
from .lrc import attach, lyrics_from_lrc, parse_lrc


class DemoPlayer:
    def __init__(self):
        data = json.loads((C.DEMO_DIR / "demo.json").read_text(encoding="utf-8"))
        self.tracks = data["tracks"]
        self.idle_ms = int(data.get("idle_ms", 15000))
        self.t0 = time.monotonic() - C.DEMO_START_MS / 1000
        self.jitter = int(C.env("DEMO_JITTER_MS", "0"))  # как у Spotify Connect: progress «дрожит»

    @staticmethod
    def _item(t: dict) -> dict:
        return {"type": "track", "id": t["id"], "name": t["title"], "duration_ms": t["duration_ms"],
                "artists": [{"name": a} for a in t["artists"]],
                "album": {"name": t["album"], "images": [{"url": "demo:" + t["cover"], "width": 640}]}}

    def _now(self) -> tuple[int | None, int]:
        cycle = sum(t["duration_ms"] for t in self.tracks) + self.idle_ms
        el = int((time.monotonic() - self.t0) * 1000) % cycle
        for i, t in enumerate(self.tracks):
            if el < t["duration_ms"]:
                return i, el
            el -= t["duration_ms"]
        return None, el

    async def player(self) -> dict | None:
        i, el = self._now()
        if i is None:
            return None
        el = max(0, el + random.randint(-self.jitter, self.jitter)) if self.jitter else el
        return {"is_playing": True, "progress_ms": el, "item": self._item(self.tracks[i])}

    def queue(self) -> list[dict]:
        i, _ = self._now()
        return [self._item(self.tracks[((i if i is not None else -1) + 1) % len(self.tracks)])]

    def lyrics(self, tid: str) -> dict | None:
        t = next((x for x in self.tracks if x["id"] == tid), None)
        if not t or not t.get("lrc"):
            return None
        lyr = lyrics_from_lrc(t["lrc"], "", "demo")
        if lyr and t.get("romalrc"):
            attach(lyr["lines"], parse_lrc(t["romalrc"])[0], "translit")
        if lyr:
            lyr.update(source_id=tid, duration_ms=t["duration_ms"])
        return lyr
