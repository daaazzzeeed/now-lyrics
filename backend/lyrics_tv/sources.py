"""Поиск текста: LRCLIB → NetEase. Сетевые ошибки отличаем от честного «не найдено»."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import httpx

from . import config as C
from .lrc import (CJK_ANY, add_gaps, attach, clean_title, copy_translit_by_text, has_translit, lyrics_from_lrc,
                  lyrics_lang, netease_duration, norm, parse_lrc, parse_yrc, pick_lrclib, pick_netease)

log = logging.getLogger("sources")

NE_HEADERS = {"Referer": "https://music.163.com/", "User-Agent": C.BROWSER_UA}
# search/get/web с 2026 года отдаёт result зашифрованной строкой — сначала cloudsearch, старые как запасные
NE_SEARCH = ("/api/cloudsearch/pc", "/api/search/get", "/api/search/get/web")
LRCLIB_HEADERS = {"User-Agent": C.UA}
RETRY_PAUSES = (1, 3)  # таймаут или 5xx: два повтора с такими паузами


class SourceDown(Exception):
    """Источник недоступен (сеть, таймаут, 5xx) — «не найдено» кэшировать нельзя."""


async def request(client: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
    """HTTP-запрос с повторами на таймаут, сетевую ошибку, 429 и 5xx. Остальные ответы — как есть."""
    for attempt in range(len(RETRY_PAUSES) + 1):
        try:
            r = await client.request(method, url, **kw)
            if r.status_code < 500 and r.status_code != 429:
                return r
            err: Exception = SourceDown(f"{r.request.url.host}: HTTP {r.status_code}")
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            err = e
        if attempt < len(RETRY_PAUSES):
            await asyncio.sleep(RETRY_PAUSES[attempt])
    raise SourceDown(f"{httpx.URL(url).host}: {err!r}") from err


def _json(resp: httpx.Response):
    if resp.status_code != 200:
        return None  # 404 и прочее — честное «нет»
    try:
        return resp.json()
    except ValueError:
        return None


def _cand(source: str, sid, title: str, artist: str, album: str, dur_ms: int, synced: bool, want_ms: int) -> dict:
    return {"source": source, "id": str(sid), "title": title, "artist": artist, "album": album,
            "duration_ms": dur_ms, "diff_ms": dur_ms - want_ms if dur_ms else None, "synced": synced}


# ── LRCLIB ──

def _lrclib_lyrics(hit: dict) -> dict | None:
    if not isinstance(hit, dict) or hit.get("instrumental"):
        return None
    lyr = lyrics_from_lrc(hit.get("syncedLyrics") or "", hit.get("plainLyrics") or "", "lrclib")
    if lyr:
        lyr["source_id"] = str(hit.get("id", ""))
        lyr["duration_ms"] = round((hit.get("duration") or 0) * 1000) or None
    return lyr


async def lrclib_stage(client: httpx.AsyncClient, info: dict, full: bool = False) -> tuple[dict | None, list[dict]]:
    """→ (текст или None, кандидаты). full — всегда спрашивать и поиск (для /debug/track)."""
    dur_ms, artist = info["duration_ms"], (info["artists"] or [""])[0]
    params = {"track_name": info["title"], "artist_name": artist, "duration": round(dur_ms / 1000)}
    if info.get("album"):
        params["album_name"] = info["album"]
    hit = _json(await request(client, "GET", f"{C.LRCLIB_BASE}/api/get", params=params, headers=LRCLIB_HEADERS))
    items = [hit] if isinstance(hit, dict) and hit.get("id") is not None else []
    if isinstance(hit, dict) and hit.get("instrumental") and not full:
        return None, []
    exact = (isinstance(hit, dict) and hit.get("syncedLyrics")
             and abs((hit.get("duration") or 0) * 1000 - dur_ms) <= 500
             and norm(hit.get("albumName") or "") == norm(info.get("album") or ""))
    if full or not exact:
        arr = _json(await request(client, "GET", f"{C.LRCLIB_BASE}/api/search",
                                  params={"track_name": clean_title(info["title"]), "artist_name": artist},
                                  headers=LRCLIB_HEADERS))
        seen = {x.get("id") for x in items}
        items += [x for x in (arr if isinstance(arr, list) else []) if isinstance(x, dict) and x.get("id") not in seen]
    cands = [_cand("lrclib", x.get("id"), x.get("trackName", ""), x.get("artistName", ""), x.get("albumName") or "",
                   round((x.get("duration") or 0) * 1000), bool(x.get("syncedLyrics")), dur_ms) for x in items]
    best = pick_lrclib(items, dur_ms, info.get("album") or "")
    return _lrclib_lyrics(best) if best else None, cands


async def lrclib_by_id(client: httpx.AsyncClient, sid: str) -> dict | None:
    return _lrclib_lyrics(_json(await request(client, "GET", f"{C.LRCLIB_BASE}/api/get/{sid}",
                                              headers=LRCLIB_HEADERS)))


# ── NetEase ──

async def netease_songs(client: httpx.AsyncClient, info: dict) -> list[dict]:
    query = f"{clean_title(info['title'])} {(info['artists'] or [''])[0]}".strip()
    for path in NE_SEARCH:
        js = _json(await request(client, "POST", f"{C.NETEASE_BASE}{path}",
                                 data={"s": query, "type": 1, "offset": 0, "limit": 10}, headers=NE_HEADERS))
        result = js.get("result") if isinstance(js, dict) else None
        if isinstance(result, dict) and isinstance(result.get("songs"), list):
            return [s for s in result["songs"] if isinstance(s, dict)]
    return []


async def netease_lyrics(client: httpx.AsyncClient, sid, dur_ms: int | None) -> dict | None:
    """Текст NetEase: v1 отдаёт и yrc (таймкоды по словам), и romalrc; старый эндпоинт — запасной."""
    js = _json(await request(client, "GET", f"{C.NETEASE_BASE}/api/song/lyric/v1", headers=NE_HEADERS,
                             params={"id": sid, "cp": "false", "lv": 0, "kv": 0, "tv": 0, "rv": 0, "yv": 0,
                                     "ytv": 0, "yrv": 0}))
    if not isinstance(js, dict) or not isinstance(js.get("lrc"), dict):
        js = _json(await request(client, "GET", f"{C.NETEASE_BASE}/api/song/lyric",
                                 params={"id": sid, "lv": -1, "kv": -1, "tv": -1, "rv": -1}, headers=NE_HEADERS))
    if not isinstance(js, dict) or js.get("nolyric") or js.get("uncollected") or js.get("pureMusic"):
        return None

    def part(key: str) -> str:
        v = js.get(key)
        return (v.get("lyric") or "") if isinstance(v, dict) else ""

    lyr = lyrics_from_lrc(part("lrc"), "", "netease")
    if not lyr:
        return None
    roma = parse_lrc(part("romalrc"))[0] if lyr["synced"] and part("romalrc") else []
    if roma:
        attach(lyr["lines"], roma, "translit")
    words = parse_yrc(part("yrc"))
    if lyr["synced"] and len(words) >= max(2, 0.6 * sum(1 for ln in lyr["lines"] if ln["text"])):
        # таймкоды по словам: строка загорается, когда её начинают петь; в длинных паузах — ♪
        copy_translit_by_text(words, lyr["lines"])
        if roma:
            attach(words, roma, "translit", tolerance=1500)
        lyr["lines"] = add_gaps(words)
        lyr["timing"] = "word"
    lyr["source_id"] = str(sid)
    lyr["duration_ms"] = dur_ms or None
    return lyr


def _ne_names(song: dict) -> str:
    return ", ".join(a.get("name", "") for a in (song.get("artists") or song.get("ar") or []) if isinstance(a, dict))


async def netease_stage(client: httpx.AsyncClient, info: dict) -> tuple[dict | None, list[dict]]:
    songs = await netease_songs(client, info)
    dur_ms = info["duration_ms"]
    cands = [_cand("netease", s.get("id"), s.get("name", ""), _ne_names(s),
                   ((s.get("album") or s.get("al") or {}) or {}).get("name", ""), netease_duration(s), True, dur_ms)
             for s in songs]
    best = pick_netease(songs, info["title"], info["artists"], dur_ms, info.get("album") or "")
    if not best:
        return None, cands
    return await netease_lyrics(client, best["id"], netease_duration(best)), cands


async def netease_by_id(client: httpx.AsyncClient, sid: str) -> dict | None:
    dur = None
    try:  # длительность записи нужна для масштабирования таймкодов
        js = _json(await request(client, "GET", f"{C.NETEASE_BASE}/api/song/detail",
                                 params={"ids": json.dumps([int(sid)])}, headers=NE_HEADERS))
        songs = (js or {}).get("songs") if isinstance(js, dict) else None
        dur = netease_duration(songs[0]) if songs else None
    except (SourceDown, ValueError, TypeError, AttributeError):
        pass
    return await netease_lyrics(client, sid, dur)


# ── всё вместе ──

def is_asian(info: dict, lyr: dict | None) -> bool:
    return bool(CJK_ANY.search(info["title"] + " " + " ".join(info["artists"]))) or \
        lyrics_lang(lyr) in ("ja", "ko", "zh")


async def find_lyrics(client: httpx.AsyncClient, info: dict, override: dict | None = None,
                      on_lrclib: Callable[[dict], Awaitable[None] | None] | None = None) -> dict:
    """→ {"lyrics", "definitive", "meta", "candidates"}.

    LRCLIB и NetEase спрашиваются параллельно. Приоритет: NetEase с таймкодами по словам (yrc),
    затем синхронизированный LRCLIB; NetEase — если в LRCLIB пусто или там нет транслитерации.
    definitive=False — какой-то источник был недоступен: «не найдено» кэшировать нельзя.
    """
    override = override or {}
    stages: dict[str, int] = {}
    cands: list[dict] = []
    ok = True
    t0 = time.monotonic()

    def mark(name: str) -> None:
        stages[name] = round((time.monotonic() - t0) * 1000)

    pinned = override.get("source"), override.get("source_id")
    if all(pinned):
        try:
            lyr = await (lrclib_by_id if pinned[0] == "lrclib" else netease_by_id)(client, pinned[1])
            mark(pinned[0])
        except SourceDown as e:
            log.info("%s недоступен: %s", pinned[0], e)
            lyr, ok = None, False
        return {"lyrics": lyr, "definitive": ok or lyr is not None, "candidates": [],
                "meta": _meta(lyr, info, stages, pinned=True)}

    async def stage(name: str, fn):
        try:
            res = await fn(client, info)
        except SourceDown as e:
            log.info("%s недоступен: %s", name, e)
            res = e
        except Exception as e:  # noqa: BLE001 — неожиданный формат ответа: не теряем остальное
            log.exception("%s: неожиданный ответ", name)
            res = e
        mark(name)
        return res

    # оба источника параллельно: NetEase нужен и для английских треков — у него бывают таймкоды по словам
    r_lrc, r_ne = await asyncio.gather(stage("lrclib", lrclib_stage), stage("netease", netease_stage))
    lyr = ne = None
    if isinstance(r_lrc, Exception):
        ok = False
    else:
        lyr, c = r_lrc
        cands += c
    if isinstance(r_ne, Exception):
        ok = False
    else:
        ne, c = r_ne
        cands += c
    if lyr and on_lrclib:
        res = on_lrclib(lyr)
        if asyncio.iscoroutine(res):
            await res
    lang = lyrics_lang(ne)
    if ne and (lyr is None
               or ne.get("timing") == "word"                      # точнее по времени — берём
               or (not lyr["synced"] and ne["synced"])
               or (lang in ("ja", "ko", "zh") and has_translit(ne) and not has_translit(lyr))
               or (is_asian(info, lyr) and ne["synced"] and lyr["synced"] and has_translit(ne))):
        lyr = ne
    return {"lyrics": lyr, "definitive": ok or lyr is not None, "candidates": cands,
            "meta": _meta(lyr, info, stages)}


def _meta(lyr: dict | None, info: dict, stages: dict, pinned: bool = False) -> dict:
    rec = (lyr or {}).get("duration_ms")
    return {"source": (lyr or {}).get("source"), "source_id": (lyr or {}).get("source_id"),
            "timing": (lyr or {}).get("timing", "line") if lyr else None,
            "record_ms": rec, "spotify_ms": info["duration_ms"],
            "diff_ms": (info["duration_ms"] - rec) if rec else None, "pinned": pinned, "stages_ms": stages}
