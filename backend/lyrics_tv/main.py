"""HTTP: экран, SSE, обложки, вход в Spotify. Запуск: python -m lyrics_tv"""
from __future__ import annotations

import asyncio
import hmac
import html
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config as C
from .cache import Cache
from .demo import DemoPlayer
from .hub import Hub, dumps
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
             C.GEMINI_MODEL if C.GEMINI_API_KEY else "ключ не задан — ромадзи только из NetEase")
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
