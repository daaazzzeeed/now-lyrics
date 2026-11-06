"""Состояние экрана, рассылка SSE и цикл опроса Spotify (останавливается, когда нет клиентов)."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from . import config as C
from .cache import Cache
from .demo import DemoPlayer
from .lrc import has_romaji, is_japanese, needs_romaji, promote_sub_to_romaji
from .media import DEFAULT_PALETTE, compute_palette, ensure_cover, fallback_palette
from .romaji import fill_romaji
from .sources import find_lyrics
from .spotify import AuthRequired, RateLimited, Spotify, parse_item

log = logging.getLogger("hub")

STALE_S = 20


def dumps(msg: dict) -> str:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))


class Hub:
    def __init__(self, client: httpx.AsyncClient, spotify: Spotify, cache: Cache, demo: DemoPlayer | None):
        self.client, self.spotify, self.cache, self.demo = client, spotify, cache, demo
        self.clients: set[asyncio.Queue] = set()
        self.wake = asyncio.Event()
        self.need_full = False
        self.last_tick = 0.0
        self.status = "connecting"
        self.track: dict | None = None
        self.lyrics: dict | None = None
        self.lyrics_state = "none"
        self.palette = dict(DEFAULT_PALETTE)
        self.pos = 0
        self.pos_mono = time.monotonic()
        self.playing = False
        self.paused_since: float | None = None
        self.auth_days: int | None = None
        self.tasks: set[asyncio.Task] = set()

    # ── клиенты ──
    def connect(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.clients.add(q)
        self.need_full = True
        self.wake.set()
        return q

    def disconnect(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def position(self) -> int:
        if self.playing:
            return int(self.pos + (time.monotonic() - self.pos_mono) * 1000)
        return int(self.pos)

    def snapshot(self) -> dict:
        stale = time.monotonic() - self.last_tick > STALE_S
        track = None
        if self.track:
            track = {k: self.track[k] for k in ("id", "title", "album", "duration_ms")}
            track["artists"] = ", ".join(self.track["artists"])
            track["cover"] = f"/cover/{self.track['id']}.jpg"
        return {
            "t": "state", "status": "connecting" if stale else self.status, "track": track,
            "lyrics": self.lyrics, "lyrics_state": self.lyrics_state, "palette": self.palette,
            "pos": self.position(), "playing": self.playing, "offset": C.OFFSET_MS, "auth_days": self.auth_days,
        }

    def _send(self, msg: dict) -> None:
        data = dumps(msg)
        for q in list(self.clients):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:  # клиент не успевает читать — пропускаем, придёт следующий state
                pass

    def push_state(self) -> None:
        self.need_full = False
        self._send(self.snapshot())

    def push_pos(self) -> None:
        self._send({"t": "pos", "pos": self.position(), "playing": self.playing})

    # ── цикл опроса ──
    async def run(self) -> None:
        errors = 0
        while True:
            if not self.clients:  # ТВ выключен — Spotify не трогаем вообще
                self.wake.clear()
                await self.wake.wait()
            self.wake.clear()
            try:
                delay = await self.tick()
                errors = 0
            except AuthRequired:
                self.set_status("auth")
                delay = 10.0
            except RateLimited as e:
                log.warning("Spotify просит подождать %s с", e.retry_after)
                delay = max(1.0, e.retry_after)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — сеть, таймауты, что угодно
                errors += 1
                delay = float(min(60, 2 ** min(errors, 6)))
                log.warning("опрос: %r", e)
            try:
                await asyncio.wait_for(self.wake.wait(), delay)
            except asyncio.TimeoutError:
                pass

    def set_status(self, status: str) -> None:
        self.last_tick = time.monotonic()
        self.auth_days = self.spotify.days_left() if not self.demo else None
        if self.status != status or self.need_full:
            self.status = status
            if status in ("auth", "setup"):
                self.playing = False
            self.push_state()

    async def tick(self) -> float:
        if not self.demo and not self.spotify.configured:
            self.set_status("setup")
            return 30.0
        js = await (self.demo or self.spotify).player()
        now = time.monotonic()
        self.last_tick = now
        days = None if self.demo else self.spotify.days_left()
        days_changed = days != self.auth_days
        self.auth_days = days
        info = parse_item(js) if js else None
        if not info:
            if self.status != "idle" or self.playing or self.need_full or days_changed:
                self.playing, self.status, self.paused_since = False, "idle", None
                self.push_state()
            return 10.0

        playing = bool(js.get("is_playing"))
        progress = int(js.get("progress_ms") or 0)
        before = self.status
        self.pos, self.pos_mono, self.playing = progress, now, playing
        if playing:
            self.paused_since = None
        elif self.paused_since is None:
            self.paused_since = now
        long_pause = not playing and now - self.paused_since > C.IDLE_AFTER_S
        self.status = "playing" if playing else ("idle" if long_pause else "paused")

        if not self.track or self.track["id"] != info["id"]:
            self.new_track(info)
            self.push_state()
        elif before != self.status or self.need_full or days_changed:
            self.push_state()
        else:
            self.push_pos()

        if self.status == "idle":
            return 10.0
        if playing:
            return 1.0 if info["duration_ms"] and info["duration_ms"] - progress < 4000 else 3.0
        return 5.0

    # ── новый трек ──
    def new_track(self, info: dict) -> None:
        self.track = info
        cached = self.cache.get(info["id"]) or {}
        self.palette = cached.get("palette") or fallback_palette(info["id"])
        self.lyrics = cached.get("lyrics")
        checked = cached.get("checked", 0)
        fetch = not self.lyrics and time.time() - checked > C.NEGATIVE_RETRY_S
        self.lyrics_state = "ready" if self.lyrics else ("loading" if fetch else "none")
        romaji = bool(self.lyrics) and not cached.get("romaji_done") and is_japanese(self.lyrics)
        if fetch or romaji or not cached.get("palette"):
            self._spawn(self.prepare(info, fetch, not cached.get("palette")))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _current(self, tid: str) -> bool:
        return bool(self.track) and self.track["id"] == tid

    async def prepare(self, info: dict, fetch: bool, need_palette: bool) -> None:
        """Обложка и палитра — параллельно; текст → показать сразу → ромадзи → дослать."""
        tid = info["id"]
        pal_task = asyncio.create_task(self._palette(info)) if need_palette else None
        try:
            await self._lyrics(info, fetch)
        except Exception:  # noqa: BLE001
            log.exception("текст для %s", tid)
            if self._current(tid) and self.lyrics_state == "loading":
                self.lyrics_state = "ready" if self.lyrics else "none"
                self.push_state()
        if pal_task:
            await pal_task

    async def _palette(self, info: dict) -> None:
        tid = info["id"]
        try:
            cover = await ensure_cover(self.client, info)
            pal = await compute_palette(cover) if cover else None
        except Exception as e:  # noqa: BLE001
            log.info("обложка/палитра %s: %s", tid, e)
            return
        if not pal:
            return
        self.cache.update(tid, palette=pal)
        if self._current(tid):
            self.palette = pal
            self.push_state()

    async def _lyrics(self, info: dict, fetch: bool) -> None:
        tid = info["id"]
        if fetch:
            if self.demo:
                lyrics, definitive = self.demo.lyrics(tid), True
            else:
                lyrics, definitive = await find_lyrics(self.client, info)
            if lyrics:
                promote_sub_to_romaji(lyrics)
            if lyrics or definitive:
                self.cache.update(tid, lyrics=lyrics, checked=time.time())
            if self._current(tid):
                self.lyrics = lyrics
                self.lyrics_state = "ready" if lyrics else "none"
                self.push_state()
            log.info("%s — %s: текст %s, ромадзи %s", ", ".join(info["artists"]), info["title"],
                     lyrics["source"] if lyrics else "—", "да" if has_romaji(lyrics) else "нет")
        else:
            lyrics = (self.cache.get(tid) or {}).get("lyrics")
        if not lyrics or not is_japanese(lyrics):
            if lyrics:
                self.cache.update(tid, romaji_done=True)
            return
        if not any(needs_romaji(ln) for ln in lyrics["lines"]):
            self.cache.update(tid, romaji_done=True)
            return
        if not await fill_romaji(self.client, lyrics):
            return  # Gemini недоступен — попробуем, когда трек заиграет снова
        self.cache.update(tid, lyrics=lyrics, romaji_done=True)
        if self._current(tid):
            self.lyrics = lyrics
            self.push_state()
