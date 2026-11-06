"""HTTP: экран, SSE, обложки, шрифты, вход в Spotify, служебные /debug/*. Запуск: python -m lyrics_tv"""
from __future__ import annotations

import asyncio
import hmac
import html
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

from . import config as C
from .cache import Cache
from .demo import DemoPlayer
from .hub import Hub, dumps
from .fonts import ensure_subset
from .lrc import LANGS
from .media import cover_path, ensure_cover
from .spotify import Spotify

log = logging.getLogger("lyrics-tv")

PING_S = 15
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#1c1f25;
color:#f2f2f2;font:18px/1.5 system-ui,sans-serif"><div style="max-width:560px;padding:32px">
<h1 style="font-size:28px;margin:0 0 12px">{title}</h1><p style="opacity:.75;margin:0">{body}</p></div></body></html>"""


def page(code: int, title: str, body: str) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=html.escape(title), body=html.escape(body)), status_code=code)


@asynccontextmanager
async def lifespan(app: FastAPI):
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True,
                               headers={"User-Agent": C.UA})
    spotify = Spotify(client)
    hub = Hub(client, spotify, Cache(C.CACHE_DB), DemoPlayer() if C.DEMO else None)
    app.state.hub, app.state.spotify = hub, spotify
    poller = asyncio.create_task(hub.run())
    mode = "ДЕМО" if C.DEMO else ("Spotify подключён" if spotify.logged_in else "нужен вход в Spotify")
    log.info("Lyrics TV на порту %s (%s); Gemini: %s", C.PORT, mode,
             ", ".join(C.GEMINI_MODELS) if C.GEMINI_API_KEY else "ключ не задан — транслитерация только из NetEase")
    if not C.ACCESS_KEY and not C.DEMO:
        log.warning("ACCESS_KEY не задан — экран будет отвечать 403")
    try:
        yield
    finally:
        poller.cancel()
        for t in list(hub.tasks):
            t.cancel()
        await client.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=C.WEB_DIR), name="static")


def key_ok(request: Request) -> bool:
    if not C.ACCESS_KEY:
        return C.DEMO  # без ключа пускаем только в демо-режиме
    return hmac.compare_digest(request.query_params.get("key", "").encode(), C.ACCESS_KEY.encode())


def forbidden() -> PlainTextResponse:
    return PlainTextResponse("Нужен правильный ключ: ?key=…", status_code=403)


def asset_version() -> str:
    return str(int(max(p.stat().st_mtime for p in C.WEB_DIR.rglob("*") if p.is_file())))


@app.get("/health")
async def health():
    return PlainTextResponse("ok")


@app.get("/")
async def index(request: Request):
    if not key_ok(request):
        return forbidden()
    text = (C.WEB_DIR / "index.html").read_text(encoding="utf-8").replace("{{v}}", asset_version())
    return HTMLResponse(text, headers={"Cache-Control": "no-store"})


@app.get("/events")
async def events(request: Request):
    if not key_ok(request):
        return forbidden()
    hub: Hub = request.app.state.hub
    q = hub.connect()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            yield f"data: {dumps(hub.snapshot())}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), PING_S)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {data}\n\n"
        finally:
            hub.disconnect(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/cover/{tid}.jpg")
async def cover(tid: str, request: Request):
    if not key_ok(request):
        return forbidden()
    if not tid.isascii() or not tid.isalnum() or len(tid) > 64:
        return PlainTextResponse("not found", status_code=404)
    path = cover_path(tid)
    hub: Hub = request.app.state.hub
    if not path.exists() and hub.track and hub.track["id"] == tid:
        try:
            await ensure_cover(hub.client, hub.track)
        except Exception as e:  # noqa: BLE001
            log.info("обложка: %s", e)
    if not path.exists():
        return PlainTextResponse("no cover", status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/font/{tid}.woff2")
async def font(tid: str, request: Request):
    """Жирное подмножество Noto Sans CJK под текущий трек (символы текста и названия)."""
    if not key_ok(request):
        return forbidden()
    hub: Hub = request.app.state.hub
    if not hub.track or hub.track["id"] != tid:
        return PlainTextResponse("not current", status_code=404)
    try:
        path = await ensure_subset(hub.client, hub.track, hub.raw, hub.lang)
    except Exception as e:  # noqa: BLE001
        log.warning("шрифт %s: %s", tid, e)
        path = None
    if not path:
        return PlainTextResponse("no font", status_code=404)
    return FileResponse(path, media_type="font/woff2", headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ── служебное: диагностика и ручные поправки (под ключом, удобно открывать с телефона) ──

def _track_id(request: Request) -> str | None:
    hub: Hub = request.app.state.hub
    return request.query_params.get("track") or (hub.track["id"] if hub.track else None)


@app.get("/debug/track")
async def debug_track(request: Request):
    """Текущий трек: источник текста, длительности, поправки и все кандидаты LRCLIB и NetEase."""
    if not key_ok(request):
        return forbidden()
    return JSONResponse(await request.app.state.hub.debug_info())


@app.get("/debug/offset")
async def debug_offset(request: Request):
    """?ms=-1500 — текст на 1.5 с раньше; ?add=250 — сдвинуть ещё; ?ms= (пусто) — сбросить. &track=ID — не текущий."""
    if not key_ok(request):
        return forbidden()
    hub: Hub = request.app.state.hub
    tid = _track_id(request)
    if not tid:
        return JSONResponse({"error": "нет трека"}, status_code=400)
    qp = request.query_params
    try:
        if "add" in qp:
            cur = hub.cache.override(tid).get("offset_ms") or 0
            ms = int(cur) + int(qp["add"])
        else:
            ms = int(qp["ms"]) if qp.get("ms") else None
    except ValueError:
        return JSONResponse({"error": "ms/add — целое число миллисекунд"}, status_code=400)
    return JSONResponse({"track": tid, "override": hub.set_offset(tid, ms)})


@app.get("/debug/source")
async def debug_source(request: Request):
    """?source=lrclib|netease&id=… — жёстко выбрать запись; ?source=auto — снова искать самому."""
    if not key_ok(request):
        return forbidden()
    hub: Hub = request.app.state.hub
    tid, qp = _track_id(request), request.query_params
    source = qp.get("source", "")
    if not tid or source not in ("lrclib", "netease", "auto") or (source != "auto" and not qp.get("id")):
        return JSONResponse({"error": "нужно source=lrclib|netease&id=… или source=auto"}, status_code=400)
    ov = hub.set_source(tid, None if source == "auto" else source, None if source == "auto" else qp["id"])
    return JSONResponse({"track": tid, "override": ov})


@app.get("/debug/lang")
async def debug_lang(request: Request):
    """?lang=yue — кантонский (Jyutping) для трека; &artist=Имя — для всех треков артиста; ?lang=auto — сброс."""
    if not key_ok(request):
        return forbidden()
    hub: Hub = request.app.state.hub
    qp = request.query_params
    lang = qp.get("lang", "")
    if lang not in (*LANGS, "auto"):
        return JSONResponse({"error": f"lang: {', '.join(LANGS)} или auto"}, status_code=400)
    tid = _track_id(request)
    key = f"artist:{qp['artist'].casefold()}" if qp.get("artist") else (f"track:{tid}" if tid else None)
    if not key:
        return JSONResponse({"error": "нет трека"}, status_code=400)
    return JSONResponse({"key": key, "override": hub.set_lang(key, None if lang == "auto" else lang, tid)})


@app.get("/debug/refresh")
async def debug_refresh(request: Request):
    """Сбросить кэш текста трека и найти заново."""
    if not key_ok(request):
        return forbidden()
    tid = _track_id(request)
    if not tid:
        return JSONResponse({"error": "нет трека"}, status_code=400)
    request.app.state.hub.refresh(tid)
    return JSONResponse({"track": tid, "refreshed": True})


def auth_guard(request: Request):
    if (request.client.host if request.client else "") not in LOOPBACK:
        return page(403, "Только через SSH-туннель",
                    "Вход в Spotify открывается с компьютера через ssh -L, см. README.")
    if C.DEMO or not request.app.state.spotify.configured:
        return page(500, "Spotify не настроен",
                    "Заполни SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET в .env, выключи DEMO и перезапусти сервис.")
    return None


@app.get("/login")
async def login(request: Request):
    if (bad := auth_guard(request)) is not None:
        return bad
    return RedirectResponse(request.app.state.spotify.authorize_url(), status_code=302)


@app.get("/callback")
async def callback(request: Request):
    if (bad := auth_guard(request)) is not None:
        return bad
    qp = request.query_params
    if qp.get("error"):
        return page(400, "Spotify не пустил", qp["error"])
    try:
        await request.app.state.spotify.finish_login(qp.get("code", ""), qp.get("state", ""))
    except Exception as e:  # noqa: BLE001
        return page(400, "Не получилось", str(e))
    request.app.state.hub.wake.set()
    return page(200, "Готово ✓", "Spotify подключён. Окно и SSH-туннель можно закрыть.")
