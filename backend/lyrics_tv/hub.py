"""Состояние экрана, рассылка SSE и цикл опроса Spotify (останавливается, когда нет клиентов).

Каждая смена трека получает номер поколения (gen). Задачи подготовки прошлого трека отменяются,
а результат применяется к экрану, только если поколение и id трека всё ещё текущие.
Следующий трек из очереди готовится заранее в фоне (self.inflight) — без отправки клиентам.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

import httpx

from . import config as C
from .cache import Cache
from .demo import DemoPlayer
from .fonts import font_urls
from .lrc import LANGS, adjust, detect_lang, han_lang, has_translit, lyrics_lang, needs_translit, \
    promote_sub_to_translit
from .media import DEFAULT_PALETTE, compute_palette, ensure_cover, fallback_palette, palette_fresh
from .sources import find_lyrics, lrclib_stage, netease_stage
from .spotify import AuthRequired, RateLimited, Spotify, parse_item
from .translate import fill_translation, needs_translation
from .translit import fill

log = logging.getLogger("hub")

STALE_S = 20
TRANSLIT_RETRY_S = (0, 20, 60)   # Gemini бывает перегружен — повторяем, пока трек играет
WATCHDOG_S = 20                  # столько ждём в loading, затем один перезапуск поиска, затем none
PREFETCH_DELAY_S = 3


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
        self.gen = 0
        self.raw: dict | None = None        # текст как в кэше (таймкоды записи)
        self.lyrics: dict | None = None     # то, что уходит клиенту (масштаб + поправка)
        self.lyrics_state = "none"
        self.lang: str | None = None
        self.meta: dict | None = None
        self.override: dict = {}
        self.palette = dict(DEFAULT_PALETTE)
        self.pos = 0
        self.pos_mono = time.monotonic()
        self.playing = False
        self.paused_since: float | None = None
        self.auth_days: int | None = None
        self.drift: list[int] = []   # последние расхождения progress Spotify с ожидаемым (для /debug/track)
        self.track_tasks: set[asyncio.Task] = set()   # задачи текущего поколения
        self.pipeline: asyncio.Task | None = None
        self.inflight: dict[str, asyncio.Task] = {}   # предзагрузка следующих треков
        self.tasks: set[asyncio.Task] = set()         # всё фоновое (для остановки сервера)

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
            track["fonts"] = font_urls(self.track, self.raw, self.lang)
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
        sent = time.monotonic()
        js = await (self.demo or self.spotify).player()
        now = time.monotonic()
        self.last_tick = now
        measured = sent + (now - sent) / 2  # progress_ms соответствует середине запроса, а не его концу
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
        if playing and self.playing and self.track and self.track["id"] == info["id"]:
            drift = progress - int(self.pos + (measured - self.pos_mono) * 1000)
            self.drift.append(drift)
            del self.drift[:-20]
            if abs(drift) > 1000:
                log.info("Spotify: позиция разошлась с ожидаемой на %d мс", drift)
        self.pos, self.pos_mono, self.playing = progress, measured, playing
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

    # ── задачи ──
    def _spawn(self, coro, track: bool = True) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        if track:
            self.track_tasks.add(task)
            task.add_done_callback(self.track_tasks.discard)
        return task

    def _live(self, tid: str, gen: int | None) -> bool:
        return gen is not None and gen == self.gen and bool(self.track) and self.track["id"] == tid

    def _present(self) -> None:
        """Пересобирает то, что видит клиент: масштаб таймкодов + ручная поправка + язык."""
        if not self.raw or not self.track:
            self.lyrics = None
            return
        view = adjust(self.raw, self.track["duration_ms"], int(self.override.get("offset_ms") or 0))
        view["lang"] = self.lang
        self.lyrics = view

    @staticmethod
    def _lang_for(info: dict, raw: dict | None, override: dict) -> str | None:
        """Ручной язык (например, yue) важнее определённого по тексту; без текста — по названию."""
        if override.get("lang") in LANGS:
            return override["lang"]
        return lyrics_lang(raw) or detect_lang([info["title"], *info["artists"]])

    @staticmethod
    def _translit_pending(raw: dict | None, lang: str | None = None) -> bool:
        return bool(raw) and any(needs_translit(ln) for ln in raw["lines"])

    # ── новый трек ──
    def new_track(self, info: dict) -> None:
        self.gen += 1
        gen, tid = self.gen, info["id"]
        for t in list(self.track_tasks):  # прошлый трек больше не нужен
            t.cancel()
        self.track_tasks.clear()
        self.track = info
        self.override = self.cache.override(tid, info["artists"])
        cached = self.cache.get(tid) or {}
        fresh_pal = palette_fresh(cached.get("palette"))
        self.palette = cached["palette"] if fresh_pal else fallback_palette(tid)
        self.raw, self.meta = cached.get("lyrics"), cached.get("meta")
        self.lang = self._lang_for(info, self.raw, self.override)
        fetch = not self.raw and time.time() - cached.get("checked", 0) > C.NEGATIVE_RETRY_S
        prefetching = tid in self.inflight
        self.lyrics_state = "ready" if self.raw else ("loading" if fetch or prefetching else "none")
        self._present()
        translit = self._translit_pending(self.raw, self.lang)
        log.info("смена трека gen=%d %s: %s — %s; кэш: текст %s, палитра %s%s", gen, tid, ", ".join(info["artists"]),
                 info["title"], "да" if self.raw else ("—" if fetch else "«нет»"), "да" if fresh_pal else "—",
                 "; готовится заранее" if prefetching else "")
        if fetch or translit or prefetching or needs_translation(self.raw):
            self.pipeline = self._spawn(self._pipeline(info, gen, fetch, translit))
        if not fresh_pal:
            self._spawn(self._palette(info, gen))
        if self.lyrics_state == "loading":
            self._spawn(self._watchdog(info, gen))
        self._spawn(self._prefetch_next(gen))

    async def _watchdog(self, info: dict, gen: int) -> None:
        tid = info["id"]
        await asyncio.sleep(WATCHDOG_S)
        if not self._live(tid, gen) or self.lyrics_state != "loading":
            return
        log.warning("gen=%d %s: всё ещё loading через %d с — перезапускаю поиск", gen, tid, WATCHDOG_S)
        if self.pipeline and not self.pipeline.done():
            self.pipeline.cancel()
        self.pipeline = self._spawn(self._pipeline(info, gen, True, False, wait_prefetch=False))
        await asyncio.sleep(WATCHDOG_S)
        if self._live(tid, gen) and self.lyrics_state == "loading":
            log.warning("gen=%d %s: текст так и не пришёл — показываю без текста", gen, tid)
            self.lyrics_state = "none"
            self.push_state()

    async def _palette(self, info: dict, gen: int | None) -> None:
        tid = info["id"]
        try:
            cover = await ensure_cover(self.client, info)
            pal = await compute_palette(cover) if cover else None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("обложка/палитра %s: %s", tid, e)
            return
        if not pal:
            return
        self.cache.update(tid, palette=pal)
        if self._live(tid, gen):
            self.palette = pal
            self.push_state()

    # ── конвейер текста: поиск → показать сразу → транслитерация (локально, Gemini — запасной) ──
    async def _pipeline(self, info: dict, gen: int | None, fetch: bool, translit: bool,
                        wait_prefetch: bool = True) -> None:
        """gen=None — предзагрузка: только кэш, без отправки клиентам."""
        tid = info["id"]
        tag = f"gen={gen} {tid}" if gen is not None else f"заранее {tid}"
        override = self.cache.override(tid, info["artists"])

        def live() -> bool:
            return self._live(tid, gen)

        if wait_prefetch and gen is not None and tid in self.inflight:
            with contextlib.suppress(Exception):  # трек уже готовится заранее — дождёмся и возьмём из кэша
                await asyncio.shield(self.inflight[tid])
            cached = self.cache.get(tid) or {}
            fetch = not cached.get("lyrics") and time.time() - cached.get("checked", 0) > C.NEGATIVE_RETRY_S
            if live() and not fetch:
                self.raw, self.meta = cached.get("lyrics"), cached.get("meta")
                self.lang = self._lang_for(info, self.raw, self.override)
                self.lyrics_state = "ready" if self.raw else "none"
                self._present()
                self.push_state()
                log.info("%s: взят из предзагрузки, клиенту ушёл lyrics_state=%s", tag, self.lyrics_state)
            translit = self._translit_pending(
                cached.get("lyrics"), self._lang_for(info, cached.get("lyrics"), override))

        if fetch:
            raw = await self._find(info, gen, tag, override)
        elif translit or needs_translation((self.cache.get(tid) or {}).get("lyrics")):
            raw = (self.cache.get(tid) or {}).get("lyrics")
        else:
            return
        if not raw:
            return
        if not self._translit_pending(raw):
            self.cache.update(tid, translit_done=True)
        # транслитерация и перевод независимы — параллельно, каждый досылает своё на экран
        await asyncio.gather(self._translit_stage(info, gen, tag, raw, override),
                             self._translation_stage(info, gen, tag, raw, override))

    def _progress(self, tid: str, gen: int | None, raw: dict) -> None:
        self.cache.update(tid, lyrics=raw)
        if self._live(tid, gen):
            self.raw = raw
            self._present()
            self.push_state()

    async def _translit_stage(self, info: dict, gen: int | None, tag: str, raw: dict, override: dict) -> None:
        tid = info["id"]
        if not self._translit_pending(raw):
            return
        han = han_lang(raw, override.get("lang"))
        known: dict[str, str] = {}
        t0 = time.monotonic()
        for pause in (TRANSLIT_RETRY_S if gen is not None else (0,)):
            if pause:
                await asyncio.sleep(pause)
                if not self._live(tid, gen):
                    return
            if await fill(self.client, raw, han, known, lambda: self._progress(tid, gen, raw)):
                self.cache.update(tid, lyrics=raw, translit_done=True)
                log.info("%s: транслитерация (%s) готова за %d мс", tag, han, (time.monotonic() - t0) * 1000)
                return
        log.info("%s: транслитерация не удалась — повтор при следующем появлении трека", tag)

    async def _translation_stage(self, info: dict, gen: int | None, tag: str, raw: dict, override: dict) -> None:
        if not needs_translation(raw):
            return
        lang = override.get("lang") if override.get("lang") in LANGS else lyrics_lang(raw)
        t0 = time.monotonic()
        if await fill_translation(self.client, raw, lang):
            self._progress(info["id"], gen, raw)
            log.info("%s: перевод (%s → %s) %s за %d мс", tag, lang or "авто", C.TRANSLATE_TO,
                     f"не нужен ({raw.get('translation_skip')})" if raw.get("translation_skip") else "готов",
                     (time.monotonic() - t0) * 1000)
        else:
            log.info("%s: перевод не удался — повтор при следующем появлении трека", tag)

    async def _find(self, info: dict, gen: int | None, tag: str, override: dict) -> dict | None:
        tid = info["id"]
        t0 = time.monotonic()
        log.info("%s: поиск текста начат", tag)

        if self.demo:
            raw = self.demo.lyrics(tid)
            res = {"lyrics": raw, "definitive": True, "candidates": [],
                   "meta": {"source": "demo", "source_id": tid, "record_ms": info["duration_ms"],
                            "spotify_ms": info["duration_ms"], "diff_ms": 0, "pinned": False, "stages_ms": {}}}
        else:
            try:
                res = await find_lyrics(self.client, info, override)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("%s: ошибка поиска", tag)
                res = {"lyrics": None, "definitive": False, "candidates": [], "meta": None}
        raw, meta = res["lyrics"], res["meta"]
        if raw:
            promote_sub_to_translit(raw)
        if raw or res["definitive"]:  # таймауты и 5xx «не найдено» не кэшируют
            self.cache.update(tid, lyrics=raw, meta=meta, checked=time.time(), translit_done=False)
        m = meta or {}
        log.info("%s: %s за %d мс (этапы %s); источник %s id=%s, запись %s мс, Spotify %s мс, разница %s мс",
                 tag, "найдено" if raw else ("не найдено" if res["definitive"] else "ошибка"),
                 (time.monotonic() - t0) * 1000, m.get("stages_ms"), m.get("source"), m.get("source_id"),
                 m.get("record_ms"), m.get("spotify_ms"), m.get("diff_ms"))
        if self._live(tid, gen):
            self.raw, self.meta = raw, meta
            self.lang = self._lang_for(info, raw, self.override)
            self.lyrics_state = "ready" if raw else "none"
            self._present()
            self.push_state()
            log.info("%s: клиенту ушёл lyrics_state=%s", tag, self.lyrics_state)
        return raw

    # ── предзагрузка следующего трека из очереди ──
    async def _prefetch_next(self, gen: int) -> None:
        await asyncio.sleep(PREFETCH_DELAY_S)  # сначала пусть начнётся работа над текущим треком
        if gen != self.gen:
            return
        try:
            items = self.demo.queue() if self.demo else await self.spotify.queue()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — очередь не обязательна (и бывает недоступна)
            log.info("очередь: %r", e)
            return
        nxt = parse_item({"item": items[0]}) if items else None
        if not nxt or (self.track and nxt["id"] == self.track["id"]) or nxt["id"] in self.inflight:
            return
        cached = self.cache.get(nxt["id"]) or {}
        fetch = not cached.get("lyrics") and time.time() - cached.get("checked", 0) > C.NEGATIVE_RETRY_S
        lang = self._lang_for(nxt, cached.get("lyrics"), self.cache.override(nxt["id"], nxt["artists"]))
        translit = self._translit_pending(cached.get("lyrics"), lang)
        pal = not palette_fresh(cached.get("palette"))
        if not (fetch or translit or pal or needs_translation(cached.get("lyrics"))):
            return
        log.info("готовлю заранее следующий трек %s: %s — %s", nxt["id"], ", ".join(nxt["artists"]), nxt["title"])

        async def work() -> None:
            await asyncio.gather(self._pipeline(nxt, None, fetch, translit),
                                 self._palette(nxt, None) if pal else asyncio.sleep(0),
                                 return_exceptions=True)

        task = self._spawn(work(), track=False)
        self.inflight[nxt["id"]] = task
        task.add_done_callback(lambda _t, k=nxt["id"]: self.inflight.pop(k, None))

    # ── ручные поправки (/debug/*) ──
    def _reload(self, tid: str) -> None:
        if self.track and self.track["id"] == tid:
            self.new_track(self.track)
            self.push_state()

    def set_offset(self, tid: str, ms: int | None) -> dict:
        ov = self.cache.set_override(f"track:{tid}", offset_ms=ms)
        if self.track and self.track["id"] == tid:
            self.override = self.cache.override(tid, self.track["artists"])
            self._present()
            self.push_state()
        return ov

    def set_source(self, tid: str, source: str | None, sid: str | None) -> dict:
        ov = self.cache.set_override(f"track:{tid}", source=source, source_id=sid)
        self.cache.forget_lyrics(tid)
        self._reload(tid)
        return ov

    def set_lang(self, key: str, lang: str | None, tid: str | None) -> dict:
        ov = self.cache.set_override(key, lang=lang)
        if tid:
            self.cache.forget_lyrics(tid)
            self._reload(tid)
        return ov

    def refresh(self, tid: str) -> None:
        self.cache.forget_lyrics(tid)
        self._reload(tid)

    async def debug_info(self) -> dict:
        info = self.track
        out: dict = {"status": self.status, "gen": self.gen, "track": info and {
            k: info[k] for k in ("id", "title", "artists", "album", "duration_ms")}}
        if not info:
            return out
        out.update(lyrics_state=self.lyrics_state, lang=self.lang, meta=self.meta, override=self.override,
                   adj=(self.lyrics or {}).get("adj"), lines=len((self.raw or {}).get("lines", [])),
                   translit=has_translit(self.raw), position_ms=self.position(), preparing=sorted(self.inflight),
                   spotify_drift_ms=self.drift[-10:])
        if self.demo:
            return out
        cands: list = []
        for name, stage in (("lrclib", lambda: lrclib_stage(self.client, info, full=True)),
                            ("netease", lambda: netease_stage(self.client, info))):
            try:
                cands += (await stage())[1]
            except Exception as e:  # noqa: BLE001
                cands.append({"source": name, "error": repr(e)})
        cands.sort(key=lambda c: (c.get("source", ""),
                                  abs(c["diff_ms"]) if c.get("diff_ms") is not None else 10 ** 9))
        out["candidates"] = cands
        return out
