"""Сквозной тест: настоящий сервер против поддельных Spotify / LRCLIB / NetEase / Gemini на localhost."""
from __future__ import annotations

import io
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parent.parent

TRACK_ID = "track42"
NE_LRC = ("[by:someone]\n[00:00.00] 作词 : 誰か\n[00:00.50] 作曲 : 誰か\n"
          "[00:10.00]テストの一行目です\n[00:15.00]二行目はここ\n[00:20.00]\n[00:24.00]三行目の歌\n")
NE_ROMA = "[00:10.10]tesuto no ichigyoume desu\n"  # romalrc только для первой строки
GEMINI_MAP = {"二行目はここ": "nigyoume wa koko", "三行目の歌": "sangyoume no uta"}


def jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (30, 110, 160)).save(buf, "JPEG")
    return buf.getvalue()


class Fake(BaseHTTPRequestHandler):
    """Один сервер изображает все внешние сервисы. Поведение меняется через флаги."""
    flags: dict = {}
    calls: dict = {}
    gemini_inputs: list = []
    img = b""

    def log_message(self, *a):
        pass

    def _count(self, path):
        Fake.calls[path] = Fake.calls.get(path, 0) + 1

    def _reply(self, code, obj=None, raw=None, ctype="application/json", headers=None):
        body = raw if raw is not None else (json.dumps(obj, ensure_ascii=False).encode() if obj is not None else b"")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path = urllib.parse.urlsplit(self.path).path
        self._count(path)
        if path == "/api/token":
            form = urllib.parse.parse_qs(raw.decode())
            if self.flags.get("invalid_grant"):
                return self._reply(400, {"error": "invalid_grant"})
            if form.get("grant_type") == ["authorization_code"]:
                return self._reply(200, {"access_token": "AT", "refresh_token": "RT2", "expires_in": 3600})
            return self._reply(200, {"access_token": "AT", "expires_in": 3600})
        if path == "/api/search/get/web":
            form = urllib.parse.parse_qs(raw.decode())
            assert form["type"] == ["1"] and form["limit"] == ["10"]
            assert self.headers.get("Referer", "").startswith("https://music.163.com")
            return self._reply(200, {"result": {"songs": [
                {"id": 1, "name": "別の歌", "artists": [{"name": "誰か"}], "duration": 201000},
                {"id": 42, "name": "テストの歌", "artists": [{"name": "テスト"}], "duration": 202000}]}})
        if path.startswith("/v1beta/models/") and path.endswith(":generateContent"):
            assert self.headers.get("x-goog-api-key") == "gkey"
            body = json.loads(raw)
            assert body["generationConfig"]["responseMimeType"] == "application/json"
            lines = json.loads(body["contents"][0]["parts"][0]["text"])
            Fake.gemini_inputs.append(lines)
            out = [GEMINI_MAP.get(s, "?") for s in lines]
            return self._reply(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(out)}]}}]})
        self._reply(404, {})

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        self._count(u.path)
        if u.path == "/v1/me/player":
            if self.flags.get("player401") or self.headers.get("Authorization") != "Bearer AT":
                return self._reply(401, {"error": {"status": 401}})
            if self.flags.get("idle"):
                return self._reply(204)
            return self._reply(200, {
                "is_playing": not self.flags.get("paused"), "progress_ms": 12000,
                "item": {"type": "track", "id": TRACK_ID, "name": "テストの歌", "duration_ms": 201000,
                         "artists": [{"name": "テスト"}],
                         "album": {"name": "アルバム", "images": [
                             {"url": f"{self.base}/img.jpg", "width": 640},
                             {"url": f"{self.base}/small.jpg", "width": 64}]}}})
        if u.path in ("/api/get", "/api/search"):
            assert self.headers.get("User-Agent", "").startswith("lyrics-tv/")
            if u.path == "/api/get":
                assert q["duration"] == ["201"]
                return self._reply(404, {"code": 404, "name": "TrackNotFound"})
            return self._reply(200, [])
        if u.path == "/api/song/lyric":
            assert q["id"] == ["42"]
            return self._reply(200, {"lrc": {"lyric": NE_LRC}, "romalrc": {"lyric": NE_ROMA}})
        if u.path == "/img.jpg":
            return self._reply(200, raw=self.img, ctype="image/jpeg")
        self._reply(404, {})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    pytest.importorskip("PIL")
    Fake.img = jpeg()
    fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    Fake.base = f"http://127.0.0.1:{fake.server_address[1]}"
    threading.Thread(target=fake.serve_forever, daemon=True).start()

    data = tmp_path_factory.mktemp("data")
    (data / "spotify_token.json").write_text(json.dumps(
        {"access_token": "AT", "refresh_token": "RT", "expires_at": time.time() + 3600,
         "authorized_at": time.time() - 175 * 86400}))
    port = free_port()
    env = dict(os.environ, HOST="127.0.0.1", PORT=str(port), ACCESS_KEY="k", DATA_DIR=str(data), DEMO="0",
               SPOTIFY_CLIENT_ID="cid", SPOTIFY_CLIENT_SECRET="secret", GEMINI_API_KEY="gkey",
               GEMINI_MODEL="fake-model", IDLE_AFTER_S="3", LOG_LEVEL="WARNING",
               SPOTIFY_ACCOUNTS_BASE=Fake.base, SPOTIFY_API_BASE=Fake.base + "/v1", LRCLIB_BASE=Fake.base,
               NETEASE_BASE=Fake.base, GEMINI_BASE=Fake.base)
    proc = subprocess.Popen([sys.executable, "-m", "lyrics_tv"], cwd=BACKEND, env=env)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield {"url": url, "proc": proc, "data": data}
    proc.terminate()
    proc.wait(10)
    fake.shutdown()


class Events:
    """Читает SSE в фоне и складывает события в очередь."""

    def __init__(self, url: str):
        self.q: queue.Queue = queue.Queue()
        self.client = httpx.Client(timeout=httpx.Timeout(30, read=None))
        self.cm = self.client.stream("GET", url + "/events?key=k")
        self.resp = self.cm.__enter__()
        self.raw: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.resp.iter_lines():
                self.raw.append(line)
                if line.startswith("data: "):
                    self.q.put(json.loads(line[6:]))
        except Exception:  # noqa: BLE001 — соединение закрыли
            pass

    def wait(self, pred, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = self.q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            if pred(m):
                return m
        raise AssertionError("не дождались нужного события")

    def close(self):
        try:
            self.cm.__exit__(None, None, None)
        finally:
            self.client.close()


def state(status=None, **kw):
    def pred(m):
        if m.get("t") != "state" or (status and m["status"] != status):
            return False
        return all(m.get(k) == v for k, v in kw.items())
    return pred


def test_access_key_and_static(srv):
    url = srv["url"]
    assert httpx.get(url + "/").status_code == 403
    assert httpx.get(url + "/?key=wrong").status_code == 403
    assert httpx.get(url + "/events").status_code == 403
    assert httpx.get(url + f"/cover/{TRACK_ID}.jpg").status_code == 403
    r = httpx.get(url + "/?key=k")
    assert r.status_code == 200 and "/static/app.js?v=" in r.text and "{{v}}" not in r.text
    assert httpx.get(url + "/static/app.js").status_code == 200
    assert httpx.get(url + "/static/../lyrics_tv/config.py").status_code == 404
    assert httpx.get(url + "/health").text == "ok"


def test_login_redirects(srv):
    r = httpx.get(srv["url"] + "/login", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(Fake.base + "/authorize?")
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)
    assert qs["redirect_uri"] == ["http://127.0.0.1:8888/callback"]
    assert qs["scope"] == ["user-read-currently-playing user-read-playback-state"]
    bad = httpx.get(srv["url"] + "/callback?code=x&state=forged")
    assert bad.status_code == 400


def test_netease_fallback_romalrc_and_gemini(srv):
    ev = Events(srv["url"])
    try:
        first = ev.wait(lambda m: m.get("t") == "state")
        assert first["track"] is None or first["track"]["id"] == TRACK_ID
        m = ev.wait(lambda m: m.get("t") == "state" and m["lyrics_state"] == "ready"
                    and m["lyrics"] and all(ln.get("romaji") for ln in m["lyrics"]["lines"] if ln["text"]))
        # палитра досылается отдельно — ждём, пока она станет синей (обложка 30,110,160)
        p = m if m["palette"]["card"] != "#5a6a7d" else ev.wait(lambda x: x.get("t") == "state")
    finally:
        ev.close()
    assert m["status"] == "playing" and m["playing"] is True
    assert m["track"] == {"id": TRACK_ID, "title": "テストの歌", "artists": "テスト", "album": "アルバム",
                          "duration_ms": 201000, "cover": f"/cover/{TRACK_ID}.jpg"}
    assert m["lyrics"]["source"] == "netease" and m["lyrics"]["synced"] is True
    lines = m["lyrics"]["lines"]
    assert [ln["text"] for ln in lines] == ["テストの一行目です", "二行目はここ", "", "三行目の歌"]  # титры отфильтрованы
    assert [ln.get("romaji") for ln in lines] == ["tesuto no ichigyoume desu", "nigyoume wa koko", None,
                                                  "sangyoume no uta"]
    assert Fake.gemini_inputs == [["二行目はここ", "三行目の歌"]]  # один запрос, только недостающие строки
    assert m["offset"] == 250
    assert m["auth_days"] is not None and m["auth_days"] <= 10  # пора показывать плашку
    assert p["palette"]

    r = httpx.get(srv["url"] + f"/cover/{TRACK_ID}.jpg?key=k")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == Fake.img
    assert Fake.calls.get("/small.jpg") is None  # берём крупную обложку


def test_cached_on_second_connect(srv):
    before = dict(Fake.calls)
    ev = Events(srv["url"])
    try:
        m = ev.wait(state(lyrics_state="ready"))
    finally:
        ev.close()
    assert m["lyrics"]["lines"][1]["romaji"] == "nigyoume wa koko"
    for path in ("/api/get", "/api/song/lyric"):
        assert Fake.calls.get(path) == before.get(path)
    assert len(Fake.gemini_inputs) == 1


def test_pause_becomes_idle(srv):
    ev = Events(srv["url"])
    try:
        Fake.flags["paused"] = True
        m = ev.wait(state("paused"))
        assert m["playing"] is False
        ev.wait(state("idle"), timeout=20)  # IDLE_AFTER_S=3
        Fake.flags.clear()
        ev.wait(state("playing"))
        Fake.flags["idle"] = True  # 204 — ничего не играет
        ev.wait(state("idle"), timeout=20)
    finally:
        Fake.flags.clear()
        ev.close()


def test_polling_stops_without_clients(srv):
    ev = Events(srv["url"])
    ev.wait(lambda m: m.get("t") in ("state", "pos"))
    ev.close()
    time.sleep(4)  # сервер замечает закрытие и засыпает после текущего ожидания
    n = Fake.calls.get("/v1/me/player", 0)
    time.sleep(8)
    assert Fake.calls.get("/v1/me/player", 0) == n
    ev = Events(srv["url"])  # и сразу просыпается при подключении
    try:
        ev.wait(lambda m: m.get("t") == "state")
        deadline = time.time() + 5
        while Fake.calls.get("/v1/me/player", 0) == n and time.time() < deadline:
            time.sleep(0.1)
        assert Fake.calls.get("/v1/me/player", 0) > n
    finally:
        ev.close()


def test_rss_under_limit(srv):
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(srv["proc"].pid)], capture_output=True, text=True)
    rss_mb = int(out.stdout.strip()) / 1024
    print(f"\nRSS сервера: {rss_mb:.1f} МБ")
    assert rss_mb < 80


def test_invalid_grant_goes_to_auth(srv):
    ev = Events(srv["url"])
    try:
        ev.wait(lambda m: m.get("t") == "state")
        Fake.flags.update(player401=True, invalid_grant=True)
        m = ev.wait(state("auth"), timeout=30)
        assert m["auth_days"] is None
    finally:
        Fake.flags.clear()
        ev.close()
    assert not (srv["data"] / "spotify_token.json").exists()
