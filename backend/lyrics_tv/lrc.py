"""Разбор LRC и подбор кандидатов. Только чистые функции — без сети, удобно тестировать."""
from __future__ import annotations

import bisect
import re
import unicodedata

TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
WORD_TS = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]", re.I)
META = re.compile(r"^\[(ar|ti|al|au|by|length|re|ve|tool|#)\s*:", re.I)
CREDIT = re.compile(
    r"^\s*(作词|作詞|作曲|编曲|編曲|制作人|製作|制作|混音|母带|和声|吉他|贝斯|鼓|钢琴|弦乐|录音|監製|监制|"
    r"出品|发行|企划|统筹|演唱|原唱|词|曲|lyrics?(\s+by)?|composed\s+by|composer|arranged\s+by|"
    r"arranger|produced\s+by|producer|written\s+by)\s*[:：]", re.I)
PURE_MUSIC = re.compile(r"纯音乐|請欣賞|请欣赏")
KANA = re.compile(r"[぀-ゟ゠-ヿㇰ-ㇿｦ-ﾟ]")
JP_TEXT = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")
CJK_ANY = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ가-힯]")
HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
HANGUL = re.compile(r"[가-힯ᄀ-ᇿ㄰-㆏]")
# частые иероглифы, которые в традиционном и упрощённом письме различаются
TRAD_ONLY = set("們個來說時會為這國對還過愛裡樣點開關問發讓聽見風車東書長門當實現經學無與後動體從頭將麼話歲夢淚邊總離誰顏戀聲憶")
SIMP_ONLY = set("们个来说时会为这国对还过爱里样点开关问发让听见风车东书长门当实现经学无与后动体从头将么话岁梦泪边总离谁颜恋声忆")

CREDIT_WINDOW_MS = 60_000
SAME_TIME_MS = 15
TRANSLIT_TOLERANCE_MS = 400
DURATION_TOLERANCE_MS = 2000
SCALE_MIN_DIFF_MS = 1000
SCALE_MAX_RATIO = 0.05
LANGS = ("ja", "ko", "zh", "yue")


def is_credit(text: str, t: int) -> bool:
    return bool(text) and t < CREDIT_WINDOW_MS and bool(CREDIT.match(text))


def parse_lrc(text: str) -> tuple[list[dict], list[str]]:
    """LRC → (строки с таймингом [{t, text, sub?}], строки без таймингов)."""
    offset = 0
    timed: list[dict] = []
    plain: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("{"):  # NetEase кладёт в начало JSON-строки с титрами
            continue
        m = OFFSET.match(line)
        if m:
            offset = int(m.group(1))
            continue
        if META.match(line):
            continue
        stamps, pos = [], 0
        while (m := TS.match(line, pos)):
            mm, ss, frac = m.groups()
            ms = int(frac.ljust(3, "0")[:3]) if frac else 0
            stamps.append((int(mm) * 60 + int(ss)) * 1000 + ms)
            pos = m.end()
        body = WORD_TS.sub("", line[pos:]).strip()
        body = re.sub(r"\s{2,}", " ", body)
        if stamps:
            for t in stamps:
                if not is_credit(body, t):
                    timed.append({"t": max(0, t - offset), "text": body})
        elif not line.startswith("["):
            plain.append(line)
    timed.sort(key=lambda x: x["t"])
    merged: list[dict] = []
    for ln in timed:  # одинаковый таймкод: вторая строка становится подписью
        if merged and ln["t"] - merged[-1]["t"] < SAME_TIME_MS:
            prev = merged[-1]
            if not prev["text"]:
                prev["text"] = ln["text"]
            elif ln["text"] and ln["text"] != prev["text"] and "sub" not in prev:
                prev["sub"] = ln["text"]
            continue
        merged.append(ln)
    return merged, plain


def lyrics_from_lrc(synced_text: str, plain_text: str, source: str) -> dict | None:
    """Синхронизированный текст, если он есть, иначе обычный. «纯音乐» — инструментал → None."""
    lines, plain = parse_lrc(synced_text) if synced_text else ([], [])
    if any(ln["text"] for ln in lines):
        if all(PURE_MUSIC.search(ln["text"]) for ln in lines if ln["text"]):
            return None
        while lines and not lines[0]["text"]:
            lines.pop(0)
        return {"synced": True, "source": source, "lines": lines}
    text = plain_text or "\n".join(plain)
    out: list[dict] = []
    for s in text.splitlines():
        s = s.strip()
        if s or (out and out[-1]["text"]):
            out.append({"text": s})
    while out and not out[-1]["text"]:
        out.pop()
    if out and not PURE_MUSIC.search(" ".join(x["text"] for x in out)):
        return {"synced": False, "source": source, "lines": out}
    return None


def attach(lines: list[dict], extra: list[dict], field: str = "translit",
           tolerance: int = TRANSLIT_TOLERANCE_MS) -> None:
    """Приклеивает строки второй дорожки (romalrc) к основной по ближайшему таймкоду."""
    extra = sorted((e for e in extra if e.get("text")), key=lambda e: e["t"])
    ts = [e["t"] for e in extra]
    for ln in lines:
        if not ln.get("text") or ln.get(field) or "t" not in ln:
            continue
        i = bisect.bisect_left(ts, ln["t"])
        cand = [extra[j] for j in (i - 1, i) if 0 <= j < len(extra)]
        cand = [c for c in cand if abs(c["t"] - ln["t"]) <= tolerance and c["text"] != ln["text"]]
        if cand:
            ln[field] = min(cand, key=lambda c: abs(c["t"] - ln["t"]))["text"]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"[\W_]+", "", s)


def clean_title(title: str) -> str:
    """«Song (feat. X) - 2011 Remaster» → «Song»: так поиск находит больше."""
    t = re.sub(r"\s*[(\[](feat\.?|ft\.?|with)\s[^)\]]*[)\]]", "", title or "", flags=re.I)
    t = re.sub(r"\s+-\s+.*\b(remaster(ed)?|version|edit|mix|live|mono|stereo|deluxe|bonus|acoustic|demo)\b.*$",
               "", t, flags=re.I)
    return t.strip() or title


# ── язык и транслитерация ──

def _texts(lyr: dict | None) -> list[str]:
    return [ln["text"] for ln in (lyr or {}).get("lines", []) if ln.get("text")]


def is_japanese(lyr: dict | None) -> bool:
    """Японский трек — если кана есть хотя бы в 10% строк."""
    texts = _texts(lyr)
    n = sum(1 for s in texts if KANA.search(s))
    return bool(texts) and n >= 1 and n >= 0.1 * len(texts)


def detect_lang(texts: list[str]) -> str | None:
    """Язык по тексту: хангыль → ko, кана → ja (даже если часть строк только из кандзи), иероглифы → zh."""
    joined = "\n".join(texts)
    if HANGUL.search(joined):
        return "ko"
    if KANA.search(joined):
        return "ja"
    if HAN.search(joined):
        return "zh"
    return None


def lyrics_lang(lyr: dict | None) -> str | None:
    texts = _texts(lyr)
    if any(HANGUL.search(s) for s in texts):
        return "ko"
    if is_japanese(lyr):
        return "ja"
    return detect_lang(texts)


def is_traditional(text: str) -> bool:
    trad = sum(1 for ch in text if ch in TRAD_ONLY)
    simp = sum(1 for ch in text if ch in SIMP_ONLY)
    return trad > simp


def has_translit(lyr: dict | None) -> bool:
    return bool(lyr) and any(ln.get("translit") for ln in lyr["lines"])


def is_syllabic(romaji: str) -> bool:
    """romalrc NetEase часто разбит по слогам: «to tsu ze n no ki su ya» вместо «totsuzen no kisu ya»."""
    tokens = re.findall(r"[a-zāēīōū]+", (romaji or "").lower())
    if len(tokens) < 4:
        return False
    short = sum(1 for t in tokens if len(t) <= 3) / len(tokens)
    avg = sum(map(len, tokens)) / len(tokens)
    return short >= 0.85 and avg <= 2.4


def script_of(lang: str | None):
    return {"ja": JP_TEXT, "ko": HANGUL, "zh": HAN, "yue": HAN}.get(lang or "")


def needs_translit(ln: dict, lang: str | None) -> bool:
    """Строка на языке трека без транслитерации — или с транслитерацией по слогам (её лучше переделать).
    Строки без CJK и хангыля (например, английские) не трогаем."""
    rx = script_of(lang)
    if not rx or not ln.get("text") or not rx.search(ln["text"]):
        return False
    tr = ln.get("translit")
    return not tr or (lang in ("ja", "ko") and is_syllabic(tr))


def promote_sub_to_translit(lyr: dict) -> None:
    """В некоторых LRC транслитерация лежит второй строкой с тем же таймкодом — переносим из sub."""
    for ln in lyr["lines"]:
        sub = ln.get("sub")
        if sub and not ln.get("translit") and CJK_ANY.search(ln.get("text", "")) and not CJK_ANY.search(sub) \
                and re.search(r"[a-z]", sub) and not re.search(r"[а-яё]", sub, re.I):
            ln["translit"] = ln.pop("sub")


def migrate(lyr: dict | None) -> dict | None:
    """Старый кэш: поле romaji → translit."""
    if not lyr:
        return lyr
    for ln in lyr.get("lines", []):
        if "romaji" in ln:
            r = ln.pop("romaji")
            ln.setdefault("translit", r)
    return lyr


# ── тайминги ──

def scale_factor(record_ms: int | None, spotify_ms: int | None) -> float:
    """Растущий сдвиг: текст от записи другой длины. Масштабируем, только если разница > 1 с и < 5%."""
    if not record_ms or not spotify_ms:
        return 1.0
    diff = abs(spotify_ms - record_ms)
    if diff <= SCALE_MIN_DIFF_MS or diff >= SCALE_MAX_RATIO * record_ms:
        return 1.0
    return spotify_ms / record_ms


def adjust(lyr: dict, spotify_ms: int | None, offset_ms: int = 0) -> dict:
    """Копия текста для клиента: таймкоды × масштаб + ручная поправка трека."""
    k = scale_factor(lyr.get("duration_ms"), spotify_ms)
    if k == 1.0 and not offset_ms:
        out = dict(lyr)
    else:
        out = dict(lyr, lines=[dict(ln, t=max(0, round(ln["t"] * k) + offset_ms)) if "t" in ln else ln
                               for ln in lyr["lines"]])
    out["adj"] = {"scale": round(k, 5), "offset_ms": offset_ms}
    return out


# ── выбор кандидатов ──

def pick_lrclib(items: list, dur_ms: int, album: str = "") -> dict | None:
    """LRCLIB: длительность ±2 с, синхронизированные в приоритете, затем ближайшая длительность,
    при равенстве — совпадение альбома."""
    want_album = norm(album)
    good = []
    for x in items:
        if not isinstance(x, dict) or not (x.get("syncedLyrics") or x.get("plainLyrics")):
            continue
        diff = abs((x.get("duration") or 0) * 1000 - dur_ms)
        if diff > DURATION_TOLERANCE_MS:
            continue
        a = norm(x.get("albumName") or "")
        album_ok = bool(want_album and a and (a == want_album or want_album in a or a in want_album))
        good.append(((0 if x.get("syncedLyrics") else 1, round(diff / 500), 0 if album_ok else 1, diff), x))
    good.sort(key=lambda g: g[0])
    return good[0][1] if good else None


def netease_duration(song: dict) -> int:
    return int(song.get("duration") or song.get("dt") or 0)


def pick_netease(songs: list, title: str, artists: list[str], dur_ms: int, album: str = "") -> dict | None:
    """NetEase: длительность ±2 с и совпал артист или название; ближе по длительности лучше,
    при равенстве — артист, альбом, название."""
    want_t = norm(clean_title(title))
    want_a = [norm(a) for a in artists if norm(a)]
    want_album = norm(album)
    good = []
    for song in songs or []:
        if not isinstance(song, dict):
            continue
        sd = netease_duration(song)
        diff = abs(sd - dur_ms) if sd else 10 ** 9
        if diff > DURATION_TOLERANCE_MS:
            continue
        names = [norm(a.get("name", "")) for a in (song.get("artists") or song.get("ar") or []) if isinstance(a, dict)]
        a_ok = any(a and (a in b or b in a) for a in names for b in want_a)
        t = norm(song.get("name", ""))
        t_ok = bool(t and want_t) and (t == want_t or t in want_t or want_t in t)
        if not (a_ok or t_ok):
            continue
        alb = norm(((song.get("album") or song.get("al") or {}) or {}).get("name", ""))
        album_ok = bool(want_album and alb and (alb == want_album or want_album in alb or alb in want_album))
        good.append(((round(diff / 500), not a_ok, not album_ok, not t_ok, diff), song))
    good.sort(key=lambda g: g[0])
    return good[0][1] if good else None
