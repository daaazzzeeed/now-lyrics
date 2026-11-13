"""Сквозной тест: настоящий сервер против поддельных Spotify / LRCLIB / NetEase / Gemini / шрифтов на localhost.

Тексты — только свои заглушки.
"""
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

NE_LRC = ("[by:someone]\n[00:00.00] 作词 : 誰か\n[00:00.50] 作曲 : 誰か\n"
          "[00:10.00]テストの一行目です\n[00:15.00]二行目はここ\n[00:20.00]\n[00:24.00]三行目の歌\n")
NE_ROMA = ("[00:10.10]tesuto no ichigyoume desu\n"   # нормальный ромадзи
           "[00:15.00]ni gyo u me wa ko ko\n")         # по слогам — переделает Gemini; третьей строки нет
GEMINI_JA = {"二行目はここ": "nigyoume wa koko", "三行目の歌": "sangyoume no uta"}

# Spotify id → (название, артист, альбом, длительность мс)
TRACKS = {
    "track42": ("テストの歌", "テスト", "アルバム", 201000),
    "trackKO": ("서울의 밤", "샘플", "앨범", 180000),
    "trackZH": ("月光", "样本", "专辑", 190000),
    "trackJA2": ("待ち時間", "テスト", "盤", 175000),
    "trackSLOW": ("Slow Song", "Nobody", "S", 170000),
    "trackEN": ("Fast Song", "Nobody", "F", 160000),
    "trackPRE": ("Pre Song", "Nobody", "P", 150000),
}
# LRCLIB /api/get по названию → (длительность с, синхронизированный текст)
LRCLIB = {
    "서울의 밤": (180, "[00:05.00]서울의 밤\n[00:10.00]I love the city\n[00:15.00]너와 함께\n"),
    "月光": (190, "[00:05.00]月光照在窗前\n[00:10.00]我想你\n"),
    "待ち時間": (175, "[00:05.00]ゆっくり待っている\n[00:10.00]まだ来ない\n"),
    "Slow Song": (170, "[00:01.00]slow words\n"),
    "Fast Song": (160, "[00:01.00]fast words\n"),
    "Pre Song": (150, "[00:01.00]prepared words\n"),
}


def jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (30, 110, 160)).save(buf, "JPEG")
    return buf.getvalue()


def item(tid: str, base: str) -> dict:
    name, artist, album, dur = TRACKS[tid]
    return {"type": "track", "id": tid, "name": name, "duration_ms": dur, "artists": [{"name": artist}],
            "album": {"name": album, "images": [{"url": f"{base}/img.jpg", "width": 640},
                                                {"url": f"{base}/small.jpg", "width": 64}]}}


def gemini_lang(prompt: str) -> str:
    for word, lang in (("Revised Romanization", "ko"), ("Jyutping", "yue"), ("Pinyin", "zh"), ("romaji", "ja")):
        if word in prompt:
            return lang
    return "?"


class Fake(BaseHTTPRequestHandler):
    """Один сервер изображает все внешние сервисы. Поведение меняется через флаги."""
    flags: dict = {}
    calls: dict = {}
    lrclib_names: list = []
    gemini_calls: list = []   # (язык, строки, подсказки)
    translate_calls: list = []  # (sl, tl, строки)
    img = b""
    font = b""
    base = ""

    def log_message(self, *a):
        pass

    def _count(self, path):
        Fake.calls[path] = Fake.calls.get(path, 0) + 1

    def _reply(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else (json.dumps(obj, ensure_ascii=False).encode() if obj is not None else b"")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
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
        if path == "/api/search/get/web":  # как настоящий NetEase с 2026 года: result зашифрован
            return self._reply(200, {"result": "35b1748964af8a7c1d883e3a6f3c773b", "code": 200})
        if path == "/api/cloudsearch/pc":
            form = urllib.parse.parse_qs(raw.decode())
            assert form["type"] == ["1"] and form["limit"] == ["10"]
            assert self.headers.get("Referer", "").startswith("https://music.163.com")
            if not form["s"][0].startswith("テストの歌"):
                return self._reply(200, {"result": {"songs": []}})
            return self._reply(200, {"result": {"songs": [
                {"id": 1, "name": "別の歌", "artists": [{"name": "誰か"}], "duration": 201000},
                {"id": 42, "name": "テストの歌", "ar": [{"name": "テスト"}], "dt": 202000}]}})
        if path == "/translate_a/single":  # Google Translate: строки через \n, язык источника в [2]
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            text = urllib.parse.parse_qs(raw.decode())["q"][0]
            Fake.translate_calls.append((q["sl"][0], q["tl"][0], text.split("\n")))
            detected = "en" if q["sl"][0] == "auto" and text.isascii() else q["sl"][0]
            out = "\n".join(f"ru:{ln}" for ln in text.split("\n"))
            return self._reply(200, [[[out, text, None, None]], None, detected])
        if path.startswith("/v1beta/models/") and path.endswith(":generateContent"):
            assert self.headers.get("x-goog-api-key") == "gkey"
            body = json.loads(raw)
            assert body["generationConfig"]["responseMimeType"] == "application/json"
            lang = gemini_lang(body["systemInstruction"]["parts"][0]["text"])
            items = json.loads(body["contents"][0]["parts"][0]["text"])
            lines = [x["text"] for x in items]
            Fake.gemini_calls.append((lang, lines, [x.get("hint") for x in items]))
            if self.flags.get("gemini_delay"):
                time.sleep(self.flags["gemini_delay"])
            out = [GEMINI_JA.get(s, f"{lang}:{s}") for s in lines]
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
            return self._reply(200, {"is_playing": not self.flags.get("paused"), "progress_ms": 12000,
                                     "item": item(self.flags.get("track", "track42"), self.base)})
        if u.path == "/v1/me/player/queue":
            nxt = self.flags.get("queue")
            return self._reply(200, {"queue": [item(nxt, self.base)] if nxt else []})
        if u.path in ("/api/get", "/api/search"):
            assert self.headers.get("User-Agent", "").startswith("lyrics-tv/")
            name = q["track_name"][0]
            if u.path == "/api/get":
                Fake.lrclib_names.append(name)
                if name == "Slow Song" and self.flags.get("lrclib_delay"):
                    time.sleep(self.flags["lrclib_delay"])
                if name not in LRCLIB:
                    return self._reply(404, {"code": 404, "name": "TrackNotFound"})
                dur, synced = LRCLIB[name]
                return self._reply(200, {"id": abs(hash(name)) % 10000, "trackName": name, "duration": dur,
                                         "albumName": "", "syncedLyrics": synced, "plainLyrics": ""})
            return self._reply(200, [])
        if u.path == "/api/song/lyric":
            assert q["id"] == ["42"]
            return self._reply(200, {"lrc": {"lyric": NE_LRC}, "romalrc": {"lyric": NE_ROMA}})
        if u.path == "/img.jpg":
            return self._reply(200, raw=self.img, ctype="image/jpeg")
        if u.path.startswith("/fonts/") and u.path.endswith("-Bold.otf"):
            return self._reply(200, raw=self.font, ctype="font/otf")
        self._reply(404, {})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    pytest.importorskip("PIL")
    Fake.img = jpeg()
    Fake.font = (BACKEND / "web" / "fonts" / "InterDisplay-Bold.woff").read_bytes()  # вместо Noto — любой шрифт
    fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    fake.daemon_threads = True
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
               NETEASE_BASE=Fake.base, GEMINI_BASE=Fake.base, FONT_BASE=Fake.base + "/fonts",
               GOOGLE_TRANSLATE_BASE=Fake.base, GOOGLE_DICT_BASE=Fake.base, TRANSLATE_TO="ru", DEEPL_API_KEY="")
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
    """Читает SSE в фоне и складывает события в очередь (с моментом получения)."""

    def __init__(self, url: str):
        self.q: queue.Queue = queue.Queue()
        self.client = httpx.Client(timeout=httpx.Timeout(30, read=None))
        self.cm = self.client.stream("GET", url + "/events?key=k")
        self.resp = self.cm.__enter__()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.resp.iter_lines():
                if line.startswith("data: "):
                    m = json.loads(line[6:])
                    m["_at"] = time.time()
                    self.q.put(m)
        except Exception:  # noqa: BLE001 — соединение закрыли
            pass

    def wait(self, pred, timeout=20, seen=None):
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = self.q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            if seen is not None:
                seen.append(m)
            if pred(m):
                return m
        raise AssertionError("не дождались нужного события")

    def drain(self, seconds: float) -> list:
        out, end = [], time.time() + seconds
        while time.time() < end:
            try:
                out.append(self.q.get(timeout=max(0.05, end - time.time())))
            except queue.Empty:
                pass
        return out

    def close(self):
        try:
            self.cm.__exit__(None, None, None)
        finally:
            self.client.close()


def state(status=None, track=None, **kw):
    def pred(m):
        if m.get("t") != "state" or (status and m["status"] != status):
            return False
        if track and (m.get("track") or {}).get("id") != track:
            return False
        return all(m.get(k) == v for k, v in kw.items())
    return pred


def texts(m):
    return [ln["text"] for ln in (m.get("lyrics") or {}).get("lines", [])]


def translits(m):
    return [ln.get("translit") for ln in (m.get("lyrics") or {}).get("lines", [])]


def tr0(m):
    return (translits(m) or [None])[0]


def play(tid):
    Fake.flags["track"] = tid


# ── доступ и вход ──

def test_access_key_and_static(srv):
    url = srv["url"]
    for path in ("/", "/?key=wrong", "/events", "/cover/track42.jpg", "/font/track42-JP.woff2", "/debug/track",
                 "/debug/offset?ms=1"):
        assert httpx.get(url + path).status_code == 403, path
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
    assert httpx.get(srv["url"] + "/callback?code=x&state=forged").status_code == 400


# ── японский: NetEase-фолбэк, romalrc, Gemini, шрифт, диагностика ──

def test_netease_fallback_romalrc_and_gemini(srv):
    play("track42")
    ev = Events(srv["url"])
    try:
        m = ev.wait(lambda m: m.get("t") == "state" and m["lyrics_state"] == "ready"
                    and all(ln.get("translit") for ln in m["lyrics"]["lines"] if ln["text"]))
    finally:
        ev.close()
    assert m["status"] == "playing" and m["playing"] is True
    tr = m["track"]
    assert (tr["id"], tr["title"], tr["artists"], tr["cover"]) == ("track42", "テストの歌", "テスト", "/cover/track42.jpg")
    assert [f["url"].split("?")[0] for f in tr["fonts"]] == ["/font/track42-JP.woff2"]
    lyr = m["lyrics"]
    assert lyr["source"] == "netease" and lyr["source_id"] == "42" and lyr["synced"] and lyr["lang"] == "ja"
    assert texts(m) == ["テストの一行目です", "二行目はここ", "", "三行目の歌"]  # титры отфильтрованы
    # строка 1 — нормальный romalrc, строка 2 — слоговый romalrc + деление на слова от cutlet, строка 4 — cutlet
    assert translits(m) == ["tesuto no ichigyoume desu", "ni gyoume wa koko", None, "san gyoume no uta"]
    assert not Fake.gemini_calls                                   # без LLM
    m = m if m["lyrics"]["lines"][0].get("translation") else None
    assert m is None or m["lyrics"]["lines"][0]["translation"] == "ru:テストの一行目です"
    assert m["offset"] == 250 and m["auth_days"] is not None and m["auth_days"] <= 10

    r = httpx.get(srv["url"] + "/cover/track42.jpg?key=k")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == Fake.img
    assert Fake.calls.get("/small.jpg") is None

    r = httpx.get(srv["url"] + tr["fonts"][0]["url"] + "&key=k", timeout=60)
    assert r.status_code == 200 and r.content[:4] == b"wOF2" and r.headers["content-type"] == "font/woff2"
    assert Fake.calls.get("/fonts/JP/NotoSansJP-Bold.otf") == 1


def test_debug_track_shows_source_and_candidates(srv):
    d = httpx.get(srv["url"] + "/debug/track?key=k", timeout=30).json()
    assert d["track"]["id"] == "track42" and d["lang"] == "ja"
    meta = d["meta"]
    assert (meta["source"], meta["source_id"], meta["record_ms"], meta["spotify_ms"], meta["diff_ms"]) == \
        ("netease", "42", 202000, 201000, -1000)
    ne = [c for c in d["candidates"] if c["source"] == "netease"]
    assert {c["id"] for c in ne} == {"1", "42"} and ne[0]["id"] == "1"  # ближайший по длительности — первым


def test_cached_on_second_connect(srv):
    before = dict(Fake.calls)
    ev = Events(srv["url"])
    try:
        m = ev.wait(state(lyrics_state="ready"))
    finally:
        ev.close()
    assert m["lyrics"]["lines"][1]["translit"] == "ni gyoume wa koko"
    for path in ("/api/get", "/api/song/lyric"):
        assert Fake.calls.get(path) == before.get(path)


def test_offset_override_shifts_lines(srv):
    ev = Events(srv["url"])
    try:
        ev.wait(state(lyrics_state="ready"))
        r = httpx.get(srv["url"] + "/debug/offset?key=k&ms=1500").json()
        assert r["override"] == {"offset_ms": 1500}
        m = ev.wait(lambda m: m.get("t") == "state" and (m.get("lyrics") or {}).get("adj", {}).get("offset_ms") == 1500)
        assert [ln["t"] for ln in m["lyrics"]["lines"]] == [11500, 16500, 21500, 25500]
        httpx.get(srv["url"] + "/debug/offset?key=k&add=-500")
        m = ev.wait(lambda m: m.get("t") == "state" and (m.get("lyrics") or {}).get("adj", {}).get("offset_ms") == 1000)
        httpx.get(srv["url"] + "/debug/offset?key=k&ms=")
        m = ev.wait(lambda m: m.get("t") == "state" and (m.get("lyrics") or {}).get("adj", {}).get("offset_ms") == 0)
        assert m["lyrics"]["lines"][0]["t"] == 10000
    finally:
        ev.close()


# ── корейский и китайский ──

def test_korean_only_korean_lines_romanized(srv):
    play("trackKO")
    ev = Events(srv["url"])
    seen: list = []
    try:
        m = ev.wait(lambda m: state(track="trackKO", lyrics_state="ready")(m) and tr0(m), seen=seen)
    finally:
        ev.close()
        play("track42")
    assert m["lyrics"]["lang"] == "ko"
    assert translits(m) == ["seourui bam", None, "neowa hamkke"]  # английскую строку не трогаем
    assert any(state(track="trackKO", lyrics_state="ready")(x) and not tr0(x) for x in seen)  # сначала текст


def test_chinese_pinyin_arrives_after_text_and_cantonese_override(srv):
    play("trackZH")
    ev = Events(srv["url"])
    try:
        first = ev.wait(state(track="trackZH", lyrics_state="ready"))
        assert first["lyrics"]["lang"] == "zh"
        m = first if tr0(first) else ev.wait(lambda m: state(track="trackZH")(m) and tr0(m))
        assert m["_at"] >= first["_at"] and translits(m) == ["yuè guāng zhào zài chuāng qián", "wǒ xiǎng nǐ"]
        r = httpx.get(srv["url"] + "/debug/lang?key=k&lang=yue").json()
        assert r["override"] == {"lang": "yue"}
        m = ev.wait(lambda m: state(track="trackZH")(m) and (tr0(m) or "").startswith("jyut6"))
        assert m["lyrics"]["lang"] == "yue"
        assert translits(m) == ["jyut6 gwong1 ziu3 zoi6 coeng1 cin4", "ngo5 soeng2 nei5"]
        assert not Fake.gemini_calls
    finally:
        httpx.get(srv["url"] + "/debug/lang?key=k&lang=auto&track=trackZH")
        ev.close()
        play("track42")


# ── гонки и задержки ──

def test_old_track_result_never_lands_on_new_track(srv):
    Fake.flags["lrclib_delay"] = 3
    play("trackSLOW")
    ev = Events(srv["url"])
    try:
        ev.wait(state(track="trackSLOW", lyrics_state="loading"))
        play("trackEN")
        m = ev.wait(state(track="trackEN", lyrics_state="ready"))
        assert texts(m) == ["fast words"]
        later = ev.drain(5)  # задача медленного трека отменена — его текст не появится
        for x in later:
            if x.get("t") == "state":
                assert (x["track"] or {}).get("id") == "trackEN" and "slow words" not in texts(x)
    finally:
        Fake.flags.pop("lrclib_delay", None)
        ev.close()
        play("track42")


def test_text_first_translit_later(srv):
    play("trackJA2")
    ev = Events(srv["url"])
    try:
        t0 = time.time()
        first = ev.wait(state(track="trackJA2", lyrics_state="ready"))
        assert first["_at"] - t0 < 3.5 and not any(translits(first))   # текст сразу, без ромадзи
        m = ev.wait(lambda m: state(track="trackJA2")(m) and all(translits(m)), timeout=20)
        assert m["_at"] > first["_at"]                                # ромадзи — отдельным событием позже
        assert translits(m) == ["yukkuri matte iru", "mada konai"]
    finally:
        ev.close()
        play("track42")


def test_next_track_is_prepared_from_queue(srv):
    play("trackEN")
    Fake.flags["queue"] = "trackPRE"
    ev = Events(srv["url"])
    try:
        ev.wait(state(track="trackEN"))
        deadline = time.time() + 10
        while "Pre Song" not in Fake.lrclib_names and time.time() < deadline:
            time.sleep(0.2)
        assert "Pre Song" in Fake.lrclib_names           # текст следующего трека искали заранее
        time.sleep(1)
        n = Fake.lrclib_names.count("Pre Song")
        play("trackPRE")
        m = ev.wait(state(track="trackPRE"))
        assert m["lyrics_state"] == "ready" and texts(m) == ["prepared words"]  # сразу из кэша
        assert Fake.lrclib_names.count("Pre Song") == n
    finally:
        Fake.flags.pop("queue", None)
        ev.close()
        play("track42")


def test_translation_for_all_songs(srv):
    play("trackKO")
    ev = Events(srv["url"])
    try:
        m = ev.wait(lambda m: state(track="trackKO", lyrics_state="ready")(m)
                    and (m["lyrics"]["lines"][0].get("translation")), timeout=20)
        assert [ln.get("translation") for ln in m["lyrics"]["lines"]] == \
            ["ru:서울의 밤", "ru:I love the city", "ru:너와 함께"]
        assert ("ko", "ru", ["서울의 밤", "I love the city", "너와 함께"]) in Fake.translate_calls
        play("trackEN")                                                 # английские тоже переводятся
        m = ev.wait(lambda m: state(track="trackEN", lyrics_state="ready")(m)
                    and m["lyrics"]["lines"][0].get("translation"), timeout=20)
        assert m["lyrics"]["lines"][0]["translation"] == "ru:fast words"
    finally:
        ev.close()
        play("track42")


# ── статусы, опрос, память, авторизация ──

def test_pause_becomes_idle(srv):
    ev = Events(srv["url"])
    try:
        Fake.flags["paused"] = True
        assert ev.wait(state("paused"))["playing"] is False
        ev.wait(state("idle"), timeout=20)  # IDLE_AFTER_S=3
        Fake.flags.pop("paused")
        ev.wait(state("playing"))
        Fake.flags["idle"] = True  # 204 — ничего не играет
        ev.wait(state("idle"), timeout=20)
    finally:
        Fake.flags.pop("paused", None)
        Fake.flags.pop("idle", None)
        ev.close()


def test_polling_stops_without_clients(srv):
    ev = Events(srv["url"])
    ev.wait(lambda m: m.get("t") in ("state", "pos"))
    ev.close()
    time.sleep(4)
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

