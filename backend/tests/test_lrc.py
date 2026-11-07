"""Юнит-тесты: LRC, титры, транслитерация, язык, кандидаты, тайминги, overrides, Gemini, палитра, шрифты."""
import asyncio
import colorsys
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from lyrics_tv import config as C
from lyrics_tv import fonts, lrc, sources, translit
from lyrics_tv.cache import Cache

BACKEND = Path(__file__).resolve().parent.parent


def run(coro):
    return asyncio.run(coro)


# ── LRC ──

def test_timestamps_offset_and_word_tags():
    lines, _ = lrc.parse_lrc("[ar:Someone]\n[offset:+500]\n[00:01.5]one\n"
                             "[00:02.05][00:10.00]two\n[01:00.123]three <00:01.00>word\n")
    assert [(x["t"], x["text"]) for x in lines] == [(1000, "one"), (1550, "two"), (9500, "two"), (59623, "three word")]


def test_empty_line_is_instrumental_pause():
    lines, _ = lrc.parse_lrc("[00:01.00]a\n[00:05.00]\n[00:09.00]b\n")
    assert [x["text"] for x in lines] == ["a", "", "b"]


def test_credits_json_and_window():
    text = ('{"t":0,"c":[{"tx":"作曲: "},{"tx":"誰か"}]}\n[00:00.00] 作词 : 誰か\n[00:00.80] 作曲 : 誰か\n'
            '[00:01.20]编曲：某人\n[00:12.00]本当の一行目\n[01:30.00]作曲: これは歌詞\n')
    lines, _ = lrc.parse_lrc(text)
    assert [x["text"] for x in lines] == ["本当の一行目", "作曲: これは歌詞"]  # титры — только в первые 60 с


def test_same_timestamp_becomes_sub_and_translit():
    lines, _ = lrc.parse_lrc("[00:05.00]こんにちは\n[00:05.00]konnichiwa\n[00:09.00]\n")
    assert lines[0] == {"t": 5000, "text": "こんにちは", "sub": "konnichiwa"}
    lyr = lrc.lyrics_from_lrc("[00:05.00]こんにちは\n[00:05.00]konnichiwa\n[00:07.00]hello\n[00:07.00]привет\n", "", "x")
    lrc.promote_sub_to_translit(lyr)
    assert lyr["lines"][0].get("translit") == "konnichiwa" and "sub" not in lyr["lines"][0]
    assert lyr["lines"][1].get("sub") == "привет"


def test_plain_and_pure_music():
    lyr = lrc.lyrics_from_lrc("", "first\n\n\nsecond\n", "x")
    assert lyr["synced"] is False and [x["text"] for x in lyr["lines"]] == ["first", "", "second"]
    assert lrc.lyrics_from_lrc("[00:00.00]纯音乐，请欣赏\n", "", "x") is None


def test_attach_translit_nearest_within_400ms():
    lines = [{"t": 1000, "text": "一"}, {"t": 5000, "text": "二"}, {"t": 9000, "text": "三"}]
    lrc.attach(lines, [{"t": 1100, "text": "ichi"}, {"t": 4500, "text": "ni"}, {"t": 8700, "text": "san"},
                       {"t": 9350, "text": "far"}])
    assert lines[0].get("translit") == "ichi"
    assert "translit" not in lines[1]
    assert lines[2].get("translit") == "san"


@pytest.mark.parametrize("src,want", [
    ("Song (feat. X) - 2011 Remaster", "Song"), ("Song [with Y]", "Song"), ("Song - Live at Budokan", "Song"),
    ("Song - Acoustic Version", "Song"), ("Re: Re:", "Re: Re:"), ("Hyper-Drive", "Hyper-Drive"),
])
def test_clean_title(src, want):
    assert lrc.clean_title(src) == want


# ── язык и транслитерация ──

def L(*texts):
    return {"lines": [{"t": i * 1000, "text": t} for i, t in enumerate(texts)]}


def test_detect_lang():
    assert lrc.lyrics_lang(L("사랑해요", "I love you", "너를")) == "ko"
    assert lrc.lyrics_lang(L("夜明けの散歩", "遠い街", "君に届く")) == "ja"   # кандзи без каны в части строк
    assert lrc.lyrics_lang(L("我爱你", "月亮代表我的心")) == "zh"
    assert lrc.lyrics_lang(L("佢唔係咁嘅人", "我哋去咗")) == "yue"            # кантонские иероглифы
    assert lrc.lyrics_lang(L("財從八方來 財 (시집가는 꿈을 꾸나?)", "我們這的憨佬仔", "香爐供台上擺")) == "zh"
    assert lrc.lyrics_lang(L("hello", "world")) is None
    assert lrc.is_traditional("我們的愛會說話") and not lrc.is_traditional("我们的爱会说话")


def test_han_reading():
    assert lrc.han_lang(L("君の名は", "宇宙")) == "ja"             # есть кана — иероглифы по-японски
    assert lrc.han_lang(L("我想你")) == "zh"
    assert lrc.han_lang(L("我想你"), "yue") == "yue"               # ручной выбор
    assert lrc.han_lang(L("佢唔係咁嘅人")) == "yue"


def test_needs_translit_per_line():
    assert lrc.needs_translit({"text": "사랑해요"})
    assert not lrc.needs_translit({"text": "I love you"})          # английские строки не трогаем
    assert lrc.needs_translit({"text": "我爱你"})
    assert lrc.needs_translit({"text": "財從八方來 (시집가는 꿈을 꾸나?)"})  # смешанная строка
    assert lrc.needs_translit({"text": "突然のキスや", "translit": "to tsu ze n no ki su ya"})
    assert not lrc.needs_translit({"text": "突然のキスや", "translit": "totsuzen no kisu ya"})


def test_netease_credits_cjk_and_rules():
    lines, _ = lrc.parse_lrc("[00:06.019]录制：粒粒\n[00:07.054]混音：龙龙\n[00:08.469]--------------------\n"
                             "[00:10.208]용감한 내 가방\n")
    assert [x["text"] for x in lines] == ["용감한 내 가방"]


def test_syllabic():
    assert lrc.is_syllabic("to tsu ze n no ki su ya a tsu i ma na za shi de")
    assert not lrc.is_syllabic("totsuzen no kisu ya atsui manazashi de")
    assert not lrc.is_syllabic("kimi no na wa")


def test_old_cache_with_romaji_is_read(tmp_path):
    cache = Cache(tmp_path / "c.db")
    cache.put("t1", {"lyrics": {"synced": True, "source": "netease", "lines": [
        {"t": 1, "text": "空", "romaji": "sora"}]}, "romaji_done": True, "checked": 1})
    data = cache.get("t1")
    assert data["lyrics"]["lines"][0] == {"t": 1, "text": "空", "translit": "sora"}
    assert data["translit_done"] is True


# ── выбор кандидатов и тайминги ──

def test_pick_lrclib_nearest_and_album():
    items = [{"id": 1, "duration": 200, "syncedLyrics": "a", "albumName": "Other"},
             {"id": 2, "duration": 201, "syncedLyrics": "b", "albumName": "Single"},
             {"id": 3, "duration": 201, "syncedLyrics": "c", "albumName": "The Album"},
             {"id": 4, "duration": 201, "plainLyrics": "plain", "albumName": "The Album"},
             {"id": 5, "duration": 204, "syncedLyrics": "far"}]
    assert lrc.pick_lrclib(items, 201_200, "The Album")["id"] == 3   # ближайшая длительность, затем альбом
    assert lrc.pick_lrclib(items[:1], 201_200)["id"] == 1             # 1.2 с — в допуске ±2 с
    assert lrc.pick_lrclib(items[4:], 201_200) is None                # 2.8 с — уже нет


def test_pick_netease():
    songs = [
        {"id": 1, "name": "別の歌", "artists": [{"name": "誰か"}], "duration": 201000},
        {"id": 2, "name": "テストの歌", "artists": [{"name": "テスト"}], "duration": 150000},
        {"id": 3, "name": "ＴＥＳＴの歌", "artists": [{"name": "Other"}], "duration": 202500},
        {"id": 4, "name": "Something", "artists": [{"name": "テスト"}], "duration": 200000},
        {"id": 5, "name": "テストの歌", "ar": [{"name": "テスト"}], "dt": 201400, "al": {"name": "盤"}},
    ]
    assert lrc.pick_netease(songs[:2], "テストの歌", ["テスト"], 201000) is None
    assert lrc.pick_netease(songs[:3], "testの歌", ["X"], 201000) is None          # другой латинский артист
    assert lrc.pick_netease(songs[:3], "testの歌", ["誰でも"], 201000)["id"] == 3   # имена в разных письменностях
    assert lrc.pick_netease(songs, "テストの歌 (feat. A)", ["テスト"], 201000, "盤")["id"] == 5


def test_scale_factor_window():
    assert lrc.scale_factor(200_000, 200_800) == 1.0                  # ≤ 1 с — не трогаем
    assert lrc.scale_factor(200_000, 203_000) == pytest.approx(1.015)  # 1.5% — масштабируем
    assert lrc.scale_factor(200_000, 220_000) == 1.0                  # 10% — это другая версия, не тянем
    assert lrc.scale_factor(None, 200_000) == 1.0


def test_adjust_scales_and_offsets():
    lyr = {"duration_ms": 100_000, "lines": [{"t": 0, "text": "a"}, {"t": 50_000, "text": "b"}, {"text": "plain"}]}
    out = lrc.adjust(lyr, 102_000, offset_ms=-500)
    assert [ln.get("t") for ln in out["lines"]] == [0, 50_500, None]
    assert out["adj"] == {"scale": 1.02, "offset_ms": -500}
    assert lyr["lines"][1]["t"] == 50_000                                # исходник не меняется


def test_overrides_track_over_artist(tmp_path):
    cache = Cache(tmp_path / "c.db")
    cache.set_override("artist:leslie cheung", lang="yue")
    cache.set_override("track:t1", offset_ms=-1200, source="lrclib", source_id="77")
    assert cache.override("t1", ["Leslie Cheung"]) == {"lang": "yue", "offset_ms": -1200, "source": "lrclib",
                                                         "source_id": "77"}
    cache.set_override("track:t1", offset_ms=None, source=None, source_id=None)
    assert cache.override("t1", []) == {}


# ── источники (поддельный HTTP) ──

LRCLIB_HIT = {"id": 9, "duration": 294, "albumName": "Expressions",
              "syncedLyrics": "[00:30.49]突然のキスや\n[00:39.91]恋のプログラムを\n", "plainLyrics": "x"}
INFO = {"id": "t", "title": "Plastic Love", "artists": ["Mariya Takeuchi"], "album": "Expressions",
        "duration_ms": 294000}


def find_with(handler, info=INFO, **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await sources.find_lyrics(client, info, **kw)
    return run(go())


def lrclib_ok(netease):
    def handler(req):
        if req.url.path == "/api/get":
            return httpx.Response(200, json=LRCLIB_HIT)
        if req.url.path == "/api/search":
            return httpx.Response(200, json=[])
        return netease(req)
    return handler


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(sources, "RETRY_PAUSES", (0, 0))


def test_netease_encrypted_search_keeps_lrclib():
    res = find_with(lrclib_ok(lambda req: httpx.Response(200, json={"result": "35b1748964af8a7c", "code": 200})))
    assert res["lyrics"]["source"] == "lrclib" and res["definitive"]
    assert res["meta"]["source_id"] == "9" and res["meta"]["record_ms"] == 294000 and res["meta"]["diff_ms"] == 0


def test_netease_garbage_lyric_keeps_lrclib():
    def ne(req):
        if "search" in req.url.path:
            return httpx.Response(200, json={"result": {"songs": [
                {"id": 7, "name": "Plastic Love", "ar": [{"name": "竹内まりや"}], "dt": 294500}]}})
        return httpx.Response(200, json={"lrc": "неожиданно строка"})
    assert find_with(lrclib_ok(ne))["lyrics"]["source"] == "lrclib"


def test_netease_cloudsearch_with_romalrc_wins():
    def ne(req):
        if req.url.path == "/api/cloudsearch/pc":
            return httpx.Response(200, json={"result": {"songs": [
                {"id": 7, "name": "Plastic Love (New-Remix)", "ar": [{"name": "竹内まりや"}], "dt": 295520}]}})
        if req.url.path == "/api/song/lyric":
            return httpx.Response(200, json={
                "lrc": {"lyric": "[00:00.00] 作词 : 竹内まりや\n[00:30.71]突然のキスや\n"},
                "romalrc": {"lyric": "[00:30.710]to tsu ze n no ki su ya\n"}})
        return httpx.Response(404)
    res = find_with(lrclib_ok(ne))
    lyr = res["lyrics"]
    assert lyr["source"] == "netease" and lyr["source_id"] == "7" and lyr["duration_ms"] == 295520
    assert lyr["lines"] == [{"t": 30710, "text": "突然のキスや", "translit": "to tsu ze n no ki su ya"}]
    assert {c["source"] for c in res["candidates"]} == {"lrclib", "netease"}


def test_lrclib_picks_nearest_of_two_candidates():
    def handler(req):
        if req.url.path == "/api/get":
            return httpx.Response(404, json={})
        if req.url.path == "/api/search":
            return httpx.Response(200, json=[
                {"id": 1, "duration": 205, "syncedLyrics": "[00:01.00]radio edit", "albumName": "X"},
                {"id": 2, "duration": 199.5, "syncedLyrics": "[00:01.00]album take", "albumName": "X"},
                {"id": 3, "duration": 201, "syncedLyrics": "[00:01.00]closest", "albumName": "X"}])
        return httpx.Response(404)
    info = dict(INFO, title="Dress Down", artists=["Kaoru Akimoto"], album="X", duration_ms=200_500)
    res = find_with(handler, info)
    assert res["lyrics"]["lines"][0]["text"] == "closest" and res["lyrics"]["source_id"] == "3"


def test_retry_on_503_then_ok():
    calls = {"n": 0}

    def handler(req):
        if req.url.path == "/api/get":
            calls["n"] += 1
            return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json=LRCLIB_HIT)
        if req.url.path == "/api/search":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"result": {"songs": []}})
    res = find_with(handler)
    assert calls["n"] == 3 and res["lyrics"]["source"] == "lrclib"


def test_timeouts_are_not_definitive():
    def handler(req):
        raise httpx.ConnectTimeout("boom", request=req)
    res = find_with(handler)
    assert res["lyrics"] is None and res["definitive"] is False


def test_pinned_source():
    def handler(req):
        if req.url.path == "/api/get/555":
            return httpx.Response(200, json=dict(LRCLIB_HIT, id=555))
        return httpx.Response(500)  # остальное трогать не должны
    res = find_with(handler, override={"source": "lrclib", "source_id": "555"})
    assert res["lyrics"]["source_id"] == "555" and res["meta"]["pinned"]


# ── Gemini ──

def gemini_with(handler, lang, texts, hints=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await translit.gemini(client, lang, texts, hints)
    return run(go())


def test_gemini_falls_back_on_overload(monkeypatch):
    monkeypatch.setattr(C, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(C, "GEMINI_MODELS", ["gemini-3.8-flash"])
    seen = []

    def handler(req):
        model = req.url.path.split("/models/")[1].split(":")[0]
        seen.append(model)
        if model == "gemini-3.8-flash":
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        if model == "gemini-flash-latest":
            return httpx.Response(404, json={"error": {"message": "gone"}})
        body = json.loads(req.content)
        assert json.loads(body["contents"][0]["parts"][0]["text"]) == [{"text": "突然のキスや",
                                                                        "hint": "to tsu ze n no ki su ya"}]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '["totsuzen no kisu ya"]'}]}}]})
    assert gemini_with(handler, "ja", ["突然のキスや"], ["to tsu ze n no ki su ya"]) == ["totsuzen no kisu ya"]
    assert seen == ["gemini-3.8-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]


def test_gemini_bad_key_does_not_try_others(monkeypatch):
    monkeypatch.setattr(C, "GEMINI_API_KEY", "k")
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(400, json={"error": {"message": "API key not valid"}})
    with pytest.raises(translit.TranslitFailed, match="API key not valid"):
        gemini_with(handler, "ja", ["突然"])
    assert len(seen) == 1


@pytest.mark.parametrize("lang,word", [("ja", "Hepburn"), ("ko", "Revised Romanization"), ("zh", "Pinyin"),
                                       ("yue", "Jyutping")])
def test_gemini_prompt_per_language(monkeypatch, lang, word):
    monkeypatch.setattr(C, "GEMINI_API_KEY", "k")
    prompts = []

    def handler(req):
        prompts.append(json.loads(req.content)["systemInstruction"]["parts"][0]["text"])
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '["x"]'}]}}]})
    gemini_with(handler, lang, ["字"])
    assert word in prompts[0]


# ── палитра ──

def test_palette_worker(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    img = tmp_path / "c.jpg"
    im = PIL.new("RGB", (64, 64), (0, 0, 0))
    im.paste((200, 40, 40), (0, 0, 64, 12))
    im.save(img)
    out = subprocess.run([sys.executable, "-m", "lyrics_tv.palette_worker", str(img)], cwd=BACKEND,
                         capture_output=True, text=True, check=True)
    pal = json.loads(out.stdout)
    r, g, b = (int(pal["card"][i:i + 2], 16) for i in (1, 3, 5))
    assert {"bg", "card", "glow"} <= set(pal) and r > g and r > b


@pytest.mark.parametrize("rgb", [(190, 225, 250), (250, 240, 170), (200, 250, 210), (255, 200, 220), (230, 230, 230)])
def test_card_contrast_on_light_covers(tmp_path, rgb):
    PIL = pytest.importorskip("PIL.Image")
    from lyrics_tv.palette_worker import contrast_white, palette
    img = tmp_path / "c.jpg"
    PIL.new("RGB", (64, 64), rgb).save(img)
    pal = palette(str(img))
    r, g, b = (int(pal["card"][i:i + 2], 16) / 255 for i in (1, 3, 5))
    assert colorsys.rgb_to_hls(r, g, b)[1] <= 0.42 + 0.01
    assert contrast_white(pal["card"]) >= 4.0 and pal["v"] == 2


# ── шрифты ──

def test_font_family_and_plan():
    tr = {"id": "x", "title": "空", "artists": [], "album": ""}
    assert fonts.plan(tr, None, "ja")[0] == "JP"
    assert fonts.plan(dict(tr, title="사랑"), None, "ko")[0] == "KR"
    assert fonts.plan(dict(tr, title="我們的愛"), None, "zh")[0] == "TC"
    assert fonts.plan(dict(tr, title="我们的爱"), None, "zh")[0] == "SC"
    assert fonts.plan(dict(tr, title="Hello"), None, None) is None
    assert fonts.plan(tr, L("夜明け"), "ja")[1] == "".join(sorted(set("空夜明け")))  # название + текст


def test_font_worker_makes_woff2(tmp_path):
    pytest.importorskip("fontTools")
    src = BACKEND / "web" / "fonts" / "InterDisplay-Bold.woff"
    chars = tmp_path / "c.txt"
    chars.write_text("Lyrics")
    out = tmp_path / "o.woff2"
    subprocess.run([sys.executable, "-m", "lyrics_tv.font_worker", str(src), str(out), str(chars)],
                   cwd=BACKEND, check=True)
    assert out.read_bytes()[:4] == b"wOF2" and out.stat().st_size < src.stat().st_size


def test_gemini_only_for_lines_local_could_not_do(monkeypatch):
    monkeypatch.setattr(C, "GEMINI_API_KEY", "k")
    calls = []

    async def worker(han, texts, hints):
        return [None if "?" in t else f"local:{t}" for t in texts]   # «?» — будто библиотека не справилась
    monkeypatch.setattr(translit, "run_worker", worker)

    async def handler(req):
        items = json.loads(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        calls.append([x["text"] for x in items])
        out = [f"llm:{x['text']}" for x in items]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(out)}]}}]})

    lyr = {"synced": True, "lines": [{"t": i * 1000, "text": t} for i, t in
                                     enumerate(["我想你", "I love you", "月?", "사랑", "星?"])]}
    updates = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await translit.fill(client, lyr, "zh", {}, lambda: updates.append(1))
    assert run(go()) is True
    assert [ln.get("translit") for ln in lyr["lines"]] == ["local:我想你", None, "llm:月?", "local:사랑", "llm:星?"]
    assert calls == [["月?", "星?"]]          # в LLM ушли только две строки, одним запросом
    assert len(updates) == 2


# ── локальная транслитерация (настоящие библиотеки) ──

def test_worker_scripts_per_run():
    pytest.importorskip("cutlet")
    from lyrics_tv.translit_worker import translit_line
    assert translit_line("財從八方來 財 (시집가는 꿈을 꾸나?)", "yue") == \
        "coi4 cung4 baat3 fong1 loi4 coi4 (sijipganeun kkumeul kkuna?)"
    assert translit_line("我想你", "zh") == "wǒ xiǎng nǐ"
    assert translit_line("용감한 내 가방, no keyring", "zh") == "yonggamhan nae gabang, no keyring"
    assert translit_line("恋のプログラムを", "zh").startswith("koi no puroguramu o")   # кана → японский


def test_worker_aligns_netease_syllables():
    from lyrics_tv.translit_worker import align_to_hint, normalize_hint
    assert normalize_hint("hi ru to yo ru ga gi ya ku no ku ra shi wo") == "hi ru to yo ru ga gya ku no ku ra shi o"
    assert align_to_hint("totsuzen no kisu ya", "to tsu ze n no ki su ya") == "totsuzen no kisu ya"
    # певческое чтение из подсказки (君 kimi, 宇宙 sora), деление на слова — от локального
    assert align_to_hint("kun wa uchuu, watashi wa hoshi", "ki mi wa so ra wa ta shi wa ho shi") == \
        "kimi wa sora watashi wa hoshi"
    assert align_to_hint("kun wa uchuu", "ki mi wa so ra") == "kun wa uchuu"     # слишком разные — не верим


def test_netease_rejects_covers_by_other_artists():
    covers = [{"id": 1, "name": "NOT CUTE ANYMORE", "ar": [{"name": "bambygu"}], "dt": 131997},
              {"id": 2, "name": "NOT CUTE ANYMORE（Cover：ILLIT）", "ar": [{"name": "XUEHAN_"}], "dt": 132077}]
    assert lrc.pick_netease(covers, "NOT CUTE ANYMORE", ["ILLIT"], 132000) is None
    assert lrc.pick_netease([{"id": 3, "name": "Plastic Love", "ar": [{"name": "竹内まりや"}], "dt": 294500}],
                            "Plastic Love", ["Mariya Takeuchi"], 294000)["id"] == 3


YRC = ('{"t":1000,"c":[{"tx":"作曲: "},{"tx":"X"}]}\n'
       "[10740,4680](10740,510,0)One (11250,480,0)more (11730,1320,0)night\n"
       "[15480,4320](15480,420,0)One (15900,570,0)more (16470,1770,0)round\n"
       "[26000,2000](26000,500,0)Late (26500,800,0)line\n")


def test_yrc_word_timing_and_gaps():
    lines = lrc.add_gaps(lrc.parse_yrc(YRC))
    assert lines == [{"t": 10740, "e": 13050, "text": "One more night"},
                     {"t": 15480, "e": 18240, "text": "One more round"},
                     {"t": 18540, "text": ""},                       # 7.7 с без пения — ♪
                     {"t": 26000, "e": 27300, "text": "Late line"}]


def test_netease_yrc_wins_over_lrclib_line_timing():
    def handler(req):
        if req.url.path == "/api/get":
            return httpx.Response(200, json=dict(LRCLIB_HIT, syncedLyrics="[00:09.93]One more might\n[00:14.61]x\n"))
        if req.url.path == "/api/search":
            return httpx.Response(200, json=[])
        if req.url.path == "/api/cloudsearch/pc":
            return httpx.Response(200, json={"result": {"songs": [
                {"id": 508052774, "name": "Plastic Love", "ar": [{"name": "Mariya Takeuchi"}], "dt": 294000}]}})
        if req.url.path == "/api/song/lyric/v1":
            return httpx.Response(200, json={"lrc": {"lyric": "[00:11.46]One more night\n[00:15.03]One more round\n"},
                                             "yrc": {"lyric": YRC}})
        return httpx.Response(404)
    res = find_with(handler)
    lyr = res["lyrics"]
    assert lyr["source"] == "netease" and lyr["timing"] == "word" and res["meta"]["timing"] == "word"
    assert lyr["lines"][0] == {"t": 10740, "e": 13050, "text": "One more night"}
