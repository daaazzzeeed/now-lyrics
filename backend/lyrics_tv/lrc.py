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

CREDIT_WINDOW_MS = 60_000
SAME_TIME_MS = 15
ROMAJI_TOLERANCE_MS = 400
DURATION_TOLERANCE_S = 3


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


def attach(lines: list[dict], extra: list[dict], field: str = "romaji",
           tolerance: int = ROMAJI_TOLERANCE_MS) -> None:
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


def is_japanese(lyr: dict | None) -> bool:
    """Японский трек — если кана есть хотя бы в 10% строк."""
    if not lyr:
        return False
    texts = [ln["text"] for ln in lyr["lines"] if ln.get("text")]
    if not texts:
        return False
    n = sum(1 for s in texts if KANA.search(s))
    return n >= 1 and n >= 0.1 * len(texts)


def has_romaji(lyr: dict | None) -> bool:
    return bool(lyr) and any(ln.get("romaji") for ln in lyr["lines"])


def needs_romaji(ln: dict) -> bool:
    return bool(ln.get("text")) and bool(JP_TEXT.search(ln["text"])) and not ln.get("romaji")


def promote_sub_to_romaji(lyr: dict) -> None:
    """В некоторых LRC ромадзи лежит второй строкой с тем же таймкодом — переносим из sub."""
    for ln in lyr["lines"]:
        sub = ln.get("sub")
        if sub and not ln.get("romaji") and KANA.search(ln.get("text", "")) and not CJK_ANY.search(sub):
            ln["romaji"] = ln.pop("sub")


def pick_lrclib(items: list, dur_s: int) -> dict | None:
    """Кандидаты LRCLIB с длительностью ±3 с, синхронизированные в приоритете."""
    good = [x for x in items if isinstance(x, dict)
            and abs((x.get("duration") or 0) - dur_s) <= DURATION_TOLERANCE_S
            and (x.get("syncedLyrics") or x.get("plainLyrics"))]
    good.sort(key=lambda x: (0 if x.get("syncedLyrics") else 1, abs((x.get("duration") or 0) - dur_s)))
    return good[0] if good else None


def pick_netease(songs: list, title: str, artists: list[str], dur_ms: int) -> dict | None:
    """Кандидат NetEase подходит, если длительность ±3 с и совпал артист или название."""
    want_t = norm(clean_title(title))
    want_a = [norm(a) for a in artists if norm(a)]
    best, best_score = None, -1.0
    for song in songs or []:
        if not isinstance(song, dict):
            continue
        sd = song.get("duration") or song.get("dt") or 0
        diff = abs(sd - dur_ms) if sd else 10 ** 9
        if diff > DURATION_TOLERANCE_S * 1000:
            continue
        names = [norm(a.get("name", "")) for a in (song.get("artists") or song.get("ar") or []) if isinstance(a, dict)]
        a_ok = any(a and (a in b or b in a) for a in names for b in want_a)
        t = norm(song.get("name", ""))
        t_ok = bool(t and want_t) and (t == want_t or t in want_t or want_t in t)
        if not (a_ok or t_ok):
            continue
        score = 5 * a_ok + 3 * t_ok + (3 - diff / 1000)
        if score > best_score:
            best, best_score = song, score
    return best
