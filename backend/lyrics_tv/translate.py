"""Перевод строк неанглийских песен (по умолчанию на русский).

Провайдеры по очереди: DeepL (если задан DEEPL_API_KEY) → Google Translate (бесплатный эндпоинт без ключа)
→ Gemini (только если оба не смогли). Перевод кэшируется вместе с текстом.
"""
from __future__ import annotations

import logging
import re

import httpx

from . import config as C
from .lrc import CJK_ANY, is_traditional
from .translit import TranslitFailed, gemini_text

log = logging.getLogger("translate")

CHUNK = 25
EN_WORDS = set("the a an and or but i you he she it we they me my your our is are was were be been am do does did "
               "not no so to of in on at for with from up down out all can can't don't i'm it's just know love "
               "baby oh yeah like get got go gonna wanna one more night time when what that this there".split())
# язык трека → код Google / DeepL
GOOGLE_SRC = {"ja": "ja", "ko": "ko", "zh": "zh-CN", "zh-tw": "zh-TW", "yue": "yue"}
DEEPL_SRC = {"ja": "JA", "ko": "KO", "zh": "ZH", "zh-tw": "ZH", "yue": "ZH"}


class TranslateFailed(Exception):
    pass


def looks_english(texts: list[str]) -> bool:
    """Латиница и много частых английских слов — перевод не нужен."""
    words = re.findall(r"[a-z']+", " ".join(texts).lower())
    if not words or CJK_ANY.search("".join(texts)) or re.search(r"[а-яё]", "".join(texts), re.I):
        return False
    return sum(1 for w in words if w in EN_WORDS) / len(words) >= 0.18


def source_lang(lang: str | None, texts: list[str]) -> str | None:
    if lang == "zh" and is_traditional("".join(texts)):
        return "zh-tw"
    return lang if lang in GOOGLE_SRC else None


async def _deepl(client: httpx.AsyncClient, src: str | None, texts: list[str]) -> tuple[list[str], str | None]:
    data: dict = {"text": texts, "target_lang": C.TRANSLATE_TO.upper()}
    if src in DEEPL_SRC:
        data["source_lang"] = DEEPL_SRC[src]
    r = await client.post(f"{C.DEEPL_BASE}/v2/translate", data=data, timeout=15,
                          headers={"Authorization": f"DeepL-Auth-Key {C.DEEPL_API_KEY}"})
    if r.status_code != 200:
        raise TranslateFailed(f"DeepL: HTTP {r.status_code}")
    tr = r.json().get("translations") or []
    if len(tr) != len(texts):
        raise TranslateFailed("DeepL: не та длина ответа")
    detected = (tr[0].get("detected_source_language") or "").lower() if tr else None
    return [x.get("text", "") for x in tr], detected


async def _google(client: httpx.AsyncClient, src: str | None, texts: list[str]) -> tuple[list[str], str | None]:
    """Неофициальный бесплатный эндпоинт: строки через \\n одним запросом, порядок сохраняется."""
    params = [("client", "gtx"), ("sl", GOOGLE_SRC.get(src or "", "auto")), ("tl", C.TRANSLATE_TO), ("dt", "t")]
    r = await client.post(f"{C.GOOGLE_TRANSLATE_BASE}/translate_a/single", params=params,
                          data={"q": "\n".join(texts)}, timeout=15, headers={"User-Agent": C.BROWSER_UA})
    if r.status_code != 200:
        raise TranslateFailed(f"Google: HTTP {r.status_code}")
    try:
        js = r.json()
        joined = "".join(seg[0] for seg in js[0] if seg and seg[0])
        detected = js[2] if len(js) > 2 and isinstance(js[2], str) else None
    except (ValueError, TypeError, IndexError) as e:
        raise TranslateFailed(f"Google: непонятный ответ {e!r}") from e
    out = joined.split("\n")
    if len(out) != len(texts):
        raise TranslateFailed(f"Google: {len(out)} строк вместо {len(texts)}")
    return [s.strip() for s in out], detected


async def _gemini(client: httpx.AsyncClient, src: str | None, texts: list[str]) -> tuple[list[str], str | None]:
    prompt = (f"Translate song lyrics into {C.TRANSLATE_TO}. Input: JSON array of lines; output: JSON array of "
              "strings of the same length, line i translates input line i. Natural, short, no notes.")
    try:
        return await gemini_text(client, prompt, texts), src
    except TranslitFailed as e:
        raise TranslateFailed(f"Gemini: {e}") from e


async def translate(client: httpx.AsyncClient, lang: str | None, texts: list[str]) -> tuple[list[str], str | None]:
    """→ (переводы, определённый язык источника). Кусками по CHUNK строк, провайдеры по очереди."""
    src = source_lang(lang, texts)
    providers = ([_deepl] if C.DEEPL_API_KEY else []) + [_google] + ([_gemini] if C.GEMINI_API_KEY else [])
    out: list[str] = []
    detected = None
    for k in range(0, len(texts), CHUNK):
        part = texts[k:k + CHUNK]
        errors = []
        for fn in providers:
            try:
                res, det = await fn(client, src, part)
                out += res
                detected = detected or det
                break
            except (TranslateFailed, httpx.HTTPError) as e:
                errors.append(repr(e))
        else:
            raise TranslateFailed("; ".join(errors))
    return out, detected


def needs_translation(lyr: dict | None) -> bool:
    if not C.TRANSLATE_TO or not lyr:
        return False
    if lyr.get("translation_to") == C.TRANSLATE_TO:  # уже переведено или решено, что не нужно
        return False
    return any(ln.get("text") for ln in lyr["lines"])


async def fill_translation(client: httpx.AsyncClient, lyr: dict, lang: str | None) -> bool:
    """Переводит все строки трека. Английские песни и песни на языке перевода не трогает. True — готово."""
    lines = lyr["lines"]
    texts = [ln["text"] for ln in lines if ln.get("text")]
    if not texts:
        return True
    if lang is None and looks_english(texts):
        lyr["translation_to"] = C.TRANSLATE_TO
        lyr["translation_skip"] = "en"
        return True
    uniq = list(dict.fromkeys(texts))  # повторы припева переводим один раз
    try:
        res, detected = await translate(client, lang, uniq)
    except TranslateFailed as e:
        log.warning("перевод: %s", e)
        return False
    det = (detected or "").lower()
    if det.startswith("en") or det.startswith(C.TRANSLATE_TO.lower()):
        lyr["translation_to"] = C.TRANSLATE_TO
        lyr["translation_skip"] = det
        return True
    tr = dict(zip(uniq, res))
    for ln in lines:
        t = tr.get(ln.get("text", ""))
        if t and t.strip() and t.strip().lower() != ln["text"].strip().lower():
            ln["translation"] = t.strip()
    lyr["translation_to"] = C.TRANSLATE_TO
    return True

