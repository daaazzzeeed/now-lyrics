"""Локальная транслитерация без LLM. Короткоживущий подпроцесс: словари библиотек живут только в нём.

    stdin:  {"han": "ja"|"zh"|"yue", "items": [{"text": "...", "hint": "слоговый romalrc"|null}, ...]}
    stdout: ["...", null, ...]   — null, если строку не удалось (тогда её доделает Gemini)

Строка режется на куски по письменности, каждый кусок транслитерируется своей системой:
хангыль — Revised Romanization (korean-romanizer), кана/кандзи — Хэпбёрн (cutlet: MeCab + UniDic),
иероглифы без каны — пиньинь с тонами (pypinyin) или Jyutping (ToJyutping). Латиница и знаки — как есть.
"""
from __future__ import annotations

import difflib
import json
import re
import sys
import unicodedata

HANGUL = "가-힯ᄀ-ᇿ㄰-㆏"
KANA = "぀-ヿㇰ-ㇿｦ-ﾟ"
HAN = "㐀-䶿一-鿿豈-﫿々〆"  # + 々 〆
RUN = re.compile(f"([{HANGUL}]+)|([{KANA}{HAN}ー]+)")
PUNCT = str.maketrans({"、": ", ", "。": ". ", "「": "\"", "」": "\"", "『": "\"", "』": "\"", "・": " ", "〜": "~",
                       "【": "[", "】": "]", "《": "\"", "》": "\"", "，": ", ", "：": ": ", "；": "; "})

_kks = _rom = _pinyin = _jyut = None


def _ja(text: str) -> str:
    """cutlet (MeCab + UniDic): правильные чтения окуригана и деление на слова."""
    global _kks
    if _kks is None:
        import cutlet
        _kks = cutlet.Cutlet()
        _kks.use_foreign_spelling = False  # «ビーチ» → «biichi», а не «beach»
    out = _kks.romaji(text).lower()
    out = re.sub(r"(?<![a-z])wo(?![a-z])", "o", out)  # частица を
    return re.sub(r"(?<![a-z])watakushi(?![a-z])", "watashi", out)  # 私 в песнях — почти всегда «watashi»


def _ko(text: str) -> str:
    global _rom
    if _rom is None:
        from korean_romanizer.romanizer import Romanizer
        _rom = Romanizer
    return _rom(text).romanize().lower()


def _zh(text: str) -> str:
    global _pinyin
    if _pinyin is None:
        import pypinyin
        _pinyin = pypinyin
    return " ".join(_pinyin.lazy_pinyin(text, style=_pinyin.Style.TONE))


def _yue(text: str) -> str:
    global _jyut
    if _jyut is None:
        import ToJyutping
        _jyut = ToJyutping
    return " ".join(j or ch for ch, j in _jyut.get_jyutping_list(text))


def _tidy(s: str) -> str:
    s = unicodedata.normalize("NFKC", s.translate(PUNCT))
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,.!?:;)\]\"]?)(?=[,.!?:;)\]]|$)", r"\1", s)
    return re.sub(r"([(\[])\s+", r"\1", s)


def translit_line(text: str, han: str) -> str:
    parts = []
    pos = 0
    for m in RUN.finditer(text):
        parts.append(text[pos:m.start()])
        ko, cjk = m.group(1), m.group(2)
        if ko:
            parts.append(_ko(ko))
        elif han == "ja" or re.search(f"[{KANA}]", cjk):
            parts.append(" " + _ja(cjk) + " ")
        elif han == "yue":
            parts.append(" " + _yue(cjk) + " ")
        else:
            parts.append(" " + _zh(cjk) + " ")
        pos = m.end()
    parts.append(text[pos:])
    return _tidy("".join(parts))


def syllabic(s: str) -> bool:
    tokens = re.findall(r"[a-zāēīōū]+", (s or "").lower())
    if len(tokens) < 4:
        return False
    short = sum(1 for t in tokens if len(t) <= 3) / len(tokens)
    return short >= 0.8 and sum(map(len, tokens)) / len(tokens) <= 2.6


def tidy_hint(hint: str) -> str:
    h = re.sub(r"(?<![a-z])wo(?![a-z])", "o", hint.lower())
    return _tidy(h)


def normalize_hint(hint: str) -> str:
    """Огрехи слогового romalrc NetEase: «gi ya ku» (маленькая ゃ) → «gyaku», частица «wo» → «o»."""
    h = " " + hint.lower() + " "
    h = re.sub(r" (sh|ch|j) i y([auo]) ", r" \1\2 ", h)
    h = re.sub(r" ([kgnhbpmr]) i y([auo]) ", r" \1y\2 ", h)
    h = re.sub(r"(?<= )(ki|gi|ni|hi|bi|pi|mi|ri) y([auo])(?= )", lambda m: m.group(1)[0] + "y" + m.group(2), h)
    h = re.sub(r"(?<= )(shi|chi|ji) y([auo])(?= )", lambda m: m.group(1)[:-1] + m.group(2), h)
    h = re.sub(r"(?<= )wo(?= )", "o", h)
    return h.strip()


def align_to_hint(local: str, hint: str) -> str:
    """Слоговый romalrc (певческие чтения, но без деления на слова) + деление на слова из локального.

    «to tsu ze n no ki su ya» + «totsuzen no kisu ya» → «totsuzen no kisu ya».
    Если чтения расходятся («sora» вместо «uchuu»), берём буквы подсказки, а пробелы — по выравниванию.
    """
    a = re.sub(r"[^a-z]", "", normalize_hint(hint))
    words = local.lower().split()
    b = "".join(re.sub(r"[^a-z]", "", w) for w in words)
    if not a or not b:
        return local
    if a == b:
        return local
    cuts, n = set(), 0  # позиции в b, где кончается слово
    for w in words[:-1]:
        n += len(re.sub(r"[^a-z]", "", w))
        cuts.add(n)
    sm = difflib.SequenceMatcher(None, b, a, autojunk=False)
    if sm.ratio() < 0.6:  # слишком разные строки — подсказке не верим
        return local
    mapped = set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        for c in cuts:
            if i1 < c <= i2 or (c == i1 and tag == "equal"):
                if tag == "equal":
                    mapped.add(j1 + (c - i1))
                else:
                    mapped.add(j1 + round((c - i1) * (j2 - j1) / max(1, i2 - i1)))
    out, prev = [], 0
    for c in sorted(x for x in mapped if 0 < x < len(a)):
        out.append(a[prev:c])
        prev = c
    out.append(a[prev:])
    return " ".join(w for w in out if w)


def main() -> None:
    task = json.loads(sys.stdin.read() or "{}")
    han = task.get("han") or "zh"
    out = []
    for item in task.get("items", []):
        text, hint = item.get("text", ""), item.get("hint")
        try:
            tr = translit_line(text, han)
            only_cjk = not re.search(r"[A-Za-z]", text)
            if hint and only_cjk and re.search(f"[{KANA}]", text):
                # romalrc уже по словам — это певческие чтения, берём как есть; по слогам — выравниваем
                tr = tidy_hint(hint) if not syllabic(hint) else align_to_hint(tr, hint)
            out.append(tr or None)
        except Exception as e:  # noqa: BLE001 — нет библиотеки или странный ввод: доделает Gemini
            print(f"translit: {e!r}", file=sys.stderr)
            out.append(None)
    sys.stdout.write(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
