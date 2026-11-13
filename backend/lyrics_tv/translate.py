"""Перевод строк песен (по умолчанию на русский).

Провайдеры по очереди: DeepL (если задан DEEPL_API_KEY) → Google Translate (бесплатный эндпоинт без ключа)
→ Gemini (только если оба не смогли). Перевод кэшируется вместе с текстом.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from . import config as C
from .lrc import CJK_ANY, is_traditional
from .translit import TranslitFailed, gemini_text

log = logging.getLogger("translate")

CHUNK_CHARS = 3500   # одним куском — чтобы переводчик видел контекст песни («baby» → «детка»)
MIN_GAP_S = 1.5     # бесплатный Google отвечает 429, если спрашивать часто
_gate = asyncio.Lock()
_last = [0.0]
# язык трека → код Google / DeepL
GOOGLE_SRC = {"ja": "ja", "ko": "ko", "zh": "zh-CN", "zh-tw": "zh-TW", "yue": "yue"}
DEEPL_SRC = {"ja": "JA", "ko": "KO", "zh": "ZH", "zh-tw": "ZH", "yue": "ZH"}


class TranslateFailed(Exception):
    pass


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


async def _paced(coro_fn):
    """Запросы к бесплатному Google по одному и не чаще раза в MIN_GAP_S."""
    async with _gate:
        wait = _last[0] + MIN_GAP_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await coro_fn()
        finally:
            _last[0] = time.monotonic()


async def _google_gtx(client: httpx.AsyncClient, src: str | None, texts: list[str]) -> tuple[list[str], str | None]:
    """Строки через \n одним запросом: переводчик видит контекст, порядок сохраняется."""
    params = [("client", "gtx"), ("sl", GOOGLE_SRC.get(src or "", "auto")), ("tl", C.TRANSLATE_TO), ("dt", "t")]
    r = await _paced(lambda: client.post(f"{C.GOOGLE_TRANSLATE_BASE}/translate_a/single", params=params,
                                         data={"q": "\n".join(texts)}, timeout=15,
                                         headers={"User-Agent": C.BROWSER_UA}))
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
    return [x.strip() for x in out], detected


async def _google_dict(client: httpx.AsyncClient, src: str | None, texts: list[str]) -> tuple[list[str], str | None]:
    """Запасной бесплатный эндпоинт: каждая строка отдельно (контекста меньше, но свой лимит запросов)."""
    out: list[str] = []
    detected = None
    for k in range(0, len(texts), 40):
        part = texts[k:k + 40]
        params = [("client", "dict-chrome-ex"), ("sl", GOOGLE_SRC.get(src or "", "auto")),
                  ("tl", C.TRANSLATE_TO)] + [("q", t) for t in part]
        r = await _paced(lambda: client.get(f"{C.GOOGLE_DICT_BASE}/translate_a/t", params=params, timeout=15,
                                            headers={"User-Agent": C.BROWSER_UA}))
        if r.status_code != 200:
            raise TranslateFailed(f"Google dict: HTTP {r.status_code}")
        try:
            js = r.json()
        except ValueError as e:
            raise TranslateFailed("Google dict: не JSON") from e
        items = [x[0] if isinstance(x, list) else x for x in (js if isinstance(js, list) else [])]
        if len(items) != len(part):
            raise TranslateFailed(f"Google dict: {len(items)} строк вместо {len(part)}")
        if detected is None and js and isinstance(js[0], list) and len(js[0]) > 1:
            detected = js[0][1]
        out += [str(x).strip() for x in items]
    return out, detected


async def _google(client: httpx.AsyncClient, src: str | None, texts: list[str]) -> tuple[list[str], str | None]:
    errors = []
    for attempt, fn in enumerate((_google_gtx, _google_gtx, _google_dict)):
        try:
            return await fn(client, src, texts)
        except (TranslateFailed, httpx.HTTPError) as e:
            errors.append(repr(e))
            if attempt == 0 and "429" in repr(e):
                await asyncio.sleep(3)
    raise TranslateFailed("; ".join(errors))


async def _gemini(client: httpx.AsyncClient, src: str | None, texts: list[str]) -> tuple[list[str], str | None]:
    prompt = (f"Translate song lyrics into {C.TRANSLATE_TO}. Input: JSON array of lines; output: JSON array of "
              "strings of the same length, line i translates input line i. Natural, short, no notes.")
    try:
        return await gemini_text(client, prompt, texts), src
    except TranslitFailed as e:
        raise TranslateFailed(f"Gemini: {e}") from e


def _chunks(texts: list[str]) -> list[list[str]]:
    out, cur, size = [], [], 0
    for t in texts:
        if cur and size + len(t) + 1 > CHUNK_CHARS:
            out.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t) + 1
    return out + ([cur] if cur else [])


async def translate(client: httpx.AsyncClient, lang: str | None, texts: list[str]) -> tuple[list[str], str | None]:
    """→ (переводы, определённый язык источника). Провайдеры по очереди: DeepL → Google → Gemini."""
    src = source_lang(lang, texts)
    providers = ([_deepl] if C.DEEPL_API_KEY else []) + [_google] + ([_gemini] if C.GEMINI_API_KEY else [])
    out: list[str] = []
    detected = None
    for part in _chunks(texts):
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
    if lyr.get("translation_to") == C.TRANSLATE_TO and lyr.get("translation_skip") != "en":
        return False  # уже переведено или песня на языке перевода (английские раньше пропускались — теперь нет)
    return any(ln.get("text") for ln in lyr["lines"])


def carry_over(old: dict, new: dict) -> dict:
    """Повторный поиск по новым правилам: тот же источник — оставляем старое (с переводом и транслитерацией),
    другой — переносим переводы совпадающих строк, чтобы не переводить заново."""
    if old.get("source") == new.get("source") and old.get("source_id") == new.get("source_id"):
        return old
    if old.get("translation_to") == C.TRANSLATE_TO and not old.get("translation_skip"):
        tr = {ln["text"]: ln["translation"] for ln in old.get("lines", []) if ln.get("translation")}
        for ln in new["lines"]:
            if ln.get("text") in tr:
                ln["translation"] = tr[ln["text"]]
    return new


async def fill_translation(client: httpx.AsyncClient, lyr: dict, lang: str | None) -> bool:
    """Переводит строки без перевода (песни на языке перевода не трогает). True — готово."""
    lines = lyr["lines"]
    texts = [ln["text"] for ln in lines if ln.get("text")]
    if not texts:
        return True
    if C.TRANSLATE_TO == "ru" and sum(1 for t in texts if re.search(r"[а-яё]", t, re.I)) > 0.6 * len(texts):
        lyr["translation_to"] = C.TRANSLATE_TO          # и так по-русски
        lyr["translation_skip"] = "ru"
        return True
    todo = list(dict.fromkeys(ln["text"] for ln in lines if ln.get("text") and not ln.get("translation")))
    if not todo:
        lyr["translation_to"] = C.TRANSLATE_TO
        return True
    try:
        res, detected = await translate(client, lang, todo)  # повторы припева — один раз
    except TranslateFailed as e:
        log.warning("перевод: %s", e)
        return False
    det = (detected or "").lower()
    if det.startswith(C.TRANSLATE_TO.lower()):
        lyr["translation_to"] = C.TRANSLATE_TO
        lyr["translation_skip"] = det
        return True
    tr = dict(zip(todo, res))
    for ln in lines:
        t = tr.get(ln.get("text", ""))
        if t and t.strip() and t.strip().lower() != ln["text"].strip().lower():
            ln["translation"] = t.strip()
    lyr["translation_to"] = C.TRANSLATE_TO
    lyr.pop("translation_skip", None)
    return True
