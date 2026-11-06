"""Поиск текста: LRCLIB → NetEase. Сетевые ошибки отличаем от честного «не найдено»."""
from __future__ import annotations

import logging

import httpx

from . import config as C
from .lrc import (CJK_ANY, attach, clean_title, has_romaji, is_japanese, lyrics_from_lrc, parse_lrc,
                  pick_lrclib, pick_netease)

log = logging.getLogger("sources")

NE_HEADERS = {"Referer": "https://music.163.com/", "User-Agent": C.BROWSER_UA}


class SourceDown(Exception):
    """Источник недоступен (сеть, таймаут, 5xx) — «не найдено» кэшировать нельзя."""


def _json(resp: httpx.Response):
    if resp.status_code >= 500 or resp.status_code == 429:
        raise SourceDown(f"{resp.request.url.host}: HTTP {resp.status_code}")
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


async def lrclib_lookup(client: httpx.AsyncClient, title: str, artist: str, album: str, dur_ms: int) -> dict | None:
    dur_s = round(dur_ms / 1000)
    params = {"track_name": title, "artist_name": artist, "duration": dur_s}
    if album:
        params["album_name"] = album
    headers = {"User-Agent": C.UA}
    hit = _json(await client.get(f"{C.LRCLIB_BASE}/api/get", params=params, headers=headers))
    if isinstance(hit, dict) and hit.get("instrumental"):
        return None
    if not (isinstance(hit, dict) and hit.get("syncedLyrics")):
        arr = _json(await client.get(f"{C.LRCLIB_BASE}/api/search",
                                     params={"track_name": clean_title(title), "artist_name": artist},
                                     headers=headers))
        best = pick_lrclib(arr if isinstance(arr, list) else [], dur_s)
        if best and (not isinstance(hit, dict) or best.get("syncedLyrics")):
            hit = best
    if not isinstance(hit, dict) or hit.get("instrumental"):
        return None
    return lyrics_from_lrc(hit.get("syncedLyrics") or "", hit.get("plainLyrics") or "", "lrclib")


async def netease_lookup(client: httpx.AsyncClient, title: str, artists: list[str], dur_ms: int) -> dict | None:
    """Неофициальный API NetEase: большая база азиатской музыки, часто с готовым romalrc."""
    query = f"{clean_title(title)} {artists[0] if artists else ''}".strip()
    js = _json(await client.post(f"{C.NETEASE_BASE}/api/search/get/web",
                                 data={"s": query, "type": 1, "offset": 0, "limit": 10}, headers=NE_HEADERS))
    songs = ((js or {}).get("result") or {}).get("songs") or [] if isinstance(js, dict) else []
    best = pick_netease(songs, title, artists, dur_ms)
    if not best:
        return None
    js = _json(await client.get(f"{C.NETEASE_BASE}/api/song/lyric",
                                params={"id": best["id"], "lv": -1, "kv": -1, "tv": -1, "rv": -1},
                                headers=NE_HEADERS))
    if not isinstance(js, dict) or js.get("nolyric") or js.get("uncollected") or js.get("pureMusic"):
        return None
    lyr = lyrics_from_lrc((js.get("lrc") or {}).get("lyric") or "", "", "netease")
    if lyr and lyr["synced"]:
        roma = (js.get("romalrc") or {}).get("lyric") or ""
        if roma:
            attach(lyr["lines"], parse_lrc(roma)[0], "romaji")
    return lyr


async def find_lyrics(client: httpx.AsyncClient, info: dict) -> tuple[dict | None, bool]:
    """→ (текст | None, definitive). definitive=False — что-то было недоступно, «не найдено» не кэшируем."""
    title, artists, dur = info["title"], info["artists"], info["duration_ms"]
    ok = True
    lyr = None
    try:
        lyr = await lrclib_lookup(client, title, artists[0] if artists else "", info["album"], dur)
    except (httpx.HTTPError, SourceDown) as e:
        log.info("LRCLIB недоступен: %s", e)
        ok = False
    asian = bool(CJK_ANY.search(title + " " + " ".join(artists))) or is_japanese(lyr)
    if lyr is None or asian or not lyr["synced"]:
        ne = None
        try:
            ne = await netease_lookup(client, title, artists, dur)
        except (httpx.HTTPError, SourceDown) as e:
            log.info("NetEase недоступен: %s", e)
            ok = False
        if ne and (lyr is None
                   or (not lyr["synced"] and ne["synced"])
                   or (is_japanese(ne) and has_romaji(ne) and not has_romaji(lyr))):
            lyr = ne
    return lyr, ok or lyr is not None
