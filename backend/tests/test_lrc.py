"""Юнит-тесты: парсер LRC, фильтр титров, склейка ромадзи, выбор кандидатов, clean_title, палитра."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lyrics_tv import lrc

BACKEND = Path(__file__).resolve().parent.parent


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
    # титры отбрасываются только в первые 60 с
    assert [x["text"] for x in lines] == ["本当の一行目", "作曲: これは歌詞"]


def test_same_timestamp_becomes_sub():
    lines, _ = lrc.parse_lrc("[00:05.00]こんにちは\n[00:05.00]konnichiwa\n[00:09.00]\n")
    assert lines[0] == {"t": 5000, "text": "こんにちは", "sub": "konnichiwa"}
    assert len(lines) == 2


def test_sub_promoted_to_romaji():
    lyr = lrc.lyrics_from_lrc("[00:05.00]こんにちは\n[00:05.00]konnichiwa\n[00:07.00]hello\n[00:07.00]привет\n", "", "x")
    lrc.promote_sub_to_romaji(lyr)
    assert lyr["lines"][0].get("romaji") == "konnichiwa" and "sub" not in lyr["lines"][0]
    assert lyr["lines"][1].get("sub") == "привет"  # не японская строка — остаётся подписью


def test_plain_and_pure_music():
    lyr = lrc.lyrics_from_lrc("", "first\n\n\nsecond\n", "x")
    assert lyr["synced"] is False
    assert [x["text"] for x in lyr["lines"]] == ["first", "", "second"]
    assert lrc.lyrics_from_lrc("[00:00.00]纯音乐，请欣赏\n", "", "x") is None


def test_attach_romaji_nearest_within_400ms():
    lines = [{"t": 1000, "text": "一"}, {"t": 5000, "text": "二"}, {"t": 9000, "text": "三"}]
    lrc.attach(lines, [{"t": 1100, "text": "ichi"}, {"t": 4500, "text": "ni"}, {"t": 8700, "text": "san"},
                       {"t": 9350, "text": "far"}], "romaji")
    assert lines[0].get("romaji") == "ichi"
    assert "romaji" not in lines[1]  # 500 мс — слишком далеко
    assert lines[2].get("romaji") == "san"  # ближайший из двух подходящих


def test_is_japanese_threshold():
    kana = {"lines": [{"text": "こんにちは"}] + [{"text": "hello"}] * 9}
    assert lrc.is_japanese(kana)
    assert not lrc.is_japanese({"lines": [{"text": "こんにちは"}] + [{"text": "hello"}] * 10})
    assert not lrc.is_japanese({"lines": [{"text": "你好世界"}] * 5})  # китайский без каны


@pytest.mark.parametrize("src,want", [
    ("Song (feat. X) - 2011 Remaster", "Song"),
    ("Song [with Y]", "Song"),
    ("Song - Live at Budokan", "Song"),
    ("Song - Acoustic Version", "Song"),
    ("Re: Re:", "Re: Re:"),
    ("Hyper-Drive", "Hyper-Drive"),
])
def test_clean_title(src, want):
    assert lrc.clean_title(src) == want


def test_pick_lrclib():
    best = lrc.pick_lrclib([{"duration": 200, "plainLyrics": "a"},
                            {"duration": 202, "syncedLyrics": "[00:01.00]a"},
                            {"duration": 250, "syncedLyrics": "[00:01.00]b"}], 201)
    assert best["duration"] == 202
    assert lrc.pick_lrclib([{"duration": 210, "syncedLyrics": "x"}], 201) is None
    assert lrc.pick_lrclib([{"duration": 199, "plainLyrics": "p"}], 201)["plainLyrics"] == "p"


def test_pick_netease():
    songs = [
        {"id": 1, "name": "別の歌", "artists": [{"name": "誰か"}], "duration": 201000},           # ничего не совпало
        {"id": 2, "name": "テストの歌", "artists": [{"name": "テスト"}], "duration": 150000},     # длительность
        {"id": 3, "name": "ＴＥＳＴの歌", "artists": [{"name": "Other"}], "duration": 202500},   # название после NFKC
        {"id": 4, "name": "Something", "artists": [{"name": "テスト"}], "duration": 200000},     # артист
    ]
    assert lrc.pick_netease(songs[:2], "テストの歌", ["テスト"], 201000) is None
    assert lrc.pick_netease(songs[:3], "testの歌", ["X"], 201000)["id"] == 3
    assert lrc.pick_netease(songs, "テストの歌 (feat. A)", ["テスト"], 201000)["id"] == 4


def test_palette_worker(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    img = tmp_path / "c.jpg"
    im = PIL.new("RGB", (64, 64), (0, 0, 0))  # почти весь чёрный, но основа — красная полоса
    im.paste((200, 40, 40), (0, 0, 64, 12))
    im.save(img)
    out = subprocess.run([sys.executable, "-m", "lyrics_tv.palette_worker", str(img)], cwd=BACKEND,
                         capture_output=True, text=True, check=True)
    pal = json.loads(out.stdout)
    assert set(pal) == {"bg", "card", "glow"}
    r, g, b = (int(pal["card"][i:i + 2], 16) for i in (1, 3, 5))
    assert r > g and r > b  # оттенок взят у красного, а не у чёрного
