"""Транслитерация строк (ромадзи, корейская романизация, пиньинь, Jyutping) через Gemini REST.

Один трек — обычно два коротких запроса: сначала строки около текущей позиции, затем остальные.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import httpx

from . import config as C
from .lrc import needs_translit, norm

log = logging.getLogger("translit")

COMMON = ("Input: JSON array of song lines in order; each item has \"text\" and optional \"hint\" "
          "(a syllable-by-syllable reading with correct readings, to be grouped into words). "
          "Output: JSON array of strings, same length, item i transliterates line i. "
          "Lowercase. Keep Latin words and punctuation as is. Never translate, merge or split lines.")
SYSTEMS = {
    "ja": "Japanese lyrics to romaji: modified Hepburn, words separated by spaces, particles wa/e/o, "
          "long vowels doubled (ou, uu) without macrons, kanji read as sung, by context.",
    "ko": "Korean lyrics to Revised Romanization by actual pronunciation (apply liaison, nasalization and "
          "other sound changes), keep the original word spacing.",
    "zh": "Mandarin Chinese lyrics to Hanyu Pinyin with tone marks, words (not syllables) separated by spaces, "
          "polyphonic characters read by context.",
    "yue": "Cantonese lyrics to Jyutping with tone numbers, syllables separated by spaces.",
}
FALLBACK_MODELS = ("gemini-flash-latest", "gemini-flash-lite-latest")
TIMEOUT = httpx.Timeout(8, connect=5)
PHASE1_LINES = 8
CHUNK_LINES = 10   # фаза 2 кусками, чтобы каждый запрос укладывался в таймаут
PARALLEL = 3


class TranslitFailed(Exception):
    pass


def _short_error(r: httpx.Response) -> str:
    try:
        return str(r.json()["error"]["message"])[:160]
    except (ValueError, KeyError, TypeError):
        return r.text[:160]


async def gemini(client: httpx.AsyncClient, lang: str, texts: list[str],
                 hints: list[str | None] | None = None) -> list[str]:
    """Один запрос; модели из GEMINI_MODEL по очереди (404/429/5xx/таймаут → следующая)."""
    if not C.GEMINI_API_KEY:
        raise TranslitFailed("GEMINI_API_KEY не задан")
    hints = hints or [None] * len(texts)
    items = [{"text": t, **({"hint": h} if h else {})} for t, h in zip(texts, hints)]
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEMS[lang] + " " + COMMON}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(items, ensure_ascii=False)}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": {"type": "ARRAY", "items": {"type": "STRING"}}},
    }
    models = list(dict.fromkeys([*C.GEMINI_MODELS, *FALLBACK_MODELS]))
    errors = []
    for model in models:
        url = f"{C.GEMINI_BASE}/v1beta/models/{model}:generateContent"
        try:
            r = await client.post(url, json=body, headers={"x-goog-api-key": C.GEMINI_API_KEY}, timeout=TIMEOUT)
        except httpx.HTTPError as e:
            errors.append(f"{model}: {e!r}")
            continue
        if r.status_code != 200:
            errors.append(f"{model}: HTTP {r.status_code} {_short_error(r)}")
            if r.status_code in (404, 429) or r.status_code >= 500:
                continue
            break  # 400/401/403 — ключ или запрос, другие модели не помогут
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            out = json.loads("".join(p.get("text", "") for p in parts))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            errors.append(f"{model}: непонятный ответ {e!r}")
            continue
        if not isinstance(out, list) or len(out) != len(texts):
            errors.append(f"{model}: длина ответа {len(out) if isinstance(out, list) else '?'} ≠ {len(texts)}")
            continue
        if model != models[0]:
            log.info("Gemini: ответила запасная модель %s", model)
        return [s.strip().lower() if isinstance(s, str) else "" for s in out]
    raise TranslitFailed("; ".join(errors))


def phases(lines: list[dict], todo: list[int], pos_ms: int | None, synced: bool) -> list[list[int]]:
    """Фаза 1 — ближайшие строки от текущей позиции, фаза 2 — остальные (уже спетые — в конце)."""
    if not todo:
        return []
    if synced and pos_ms is not None:
        ahead = [i for i in todo if lines[i].get("t", 0) >= pos_ms - 3000]
        past = [i for i in todo if i not in set(ahead)]
    else:
        ahead, past = list(todo), []
    first = ahead[:PHASE1_LINES]
    rest = ahead[PHASE1_LINES:] + past
    if len(first) < PHASE1_LINES and rest and not ahead[PHASE1_LINES:]:  # конец трека: всё одним запросом
        return [first + rest]
    return [p for p in (first, rest) if p]


def apply_known(lines: list[dict], lang: str, known: dict[str, str]) -> int:
    """Дописывает транслитерацию из уже полученных ответов (по нормализованному тексту строки)."""
    n = 0
    for ln in lines:
        if needs_translit(ln, lang):
            tr = known.get(norm(ln["text"]))
            if tr:
                ln["translit"] = tr
                n += 1
    return n


async def fill(client: httpx.AsyncClient, lyr: dict, lang: str, pos_ms: Callable[[], int | None],
               known: dict[str, str], on_phase: Callable[[], None] | None = None) -> bool:
    """Транслитерация недостающих строк: фаза 1 — строки у текущей позиции, фаза 2 — остальное кусками
    по CHUNK_LINES параллельно (каждый запрос укладывается в таймаут 8 с). True — всё готово."""
    lines = lyr["lines"]
    if apply_known(lines, lang, known) and on_phase:
        on_phase()
    todo = [i for i, ln in enumerate(lines) if needs_translit(ln, lang)]
    sem = asyncio.Semaphore(PARALLEL)

    async def one(part: list[int]) -> bool:
        part = [i for i in part if needs_translit(lines[i], lang)]  # могло заполниться из known
        if not part:
            return True
        async with sem:
            t0 = time.monotonic()
            try:
                out = await gemini(client, lang, [lines[i]["text"] for i in part],
                                   [lines[i].get("translit") for i in part])  # слоговый romalrc — подсказка
            except TranslitFailed as e:
                log.warning("Gemini (%s): %s", lang, e)
                return False
        for i, r in zip(part, out):
            if r:
                lines[i]["translit"] = r
                known[norm(lines[i]["text"])] = r
        log.info("Gemini (%s): %d строк за %d мс", lang, len(part), (time.monotonic() - t0) * 1000)
        if on_phase:
            on_phase()
        return True

    ok = True
    for n, part in enumerate(phases(lines, todo, pos_ms(), lyr.get("synced", False))):
        chunks = [part[k:k + CHUNK_LINES] for k in range(0, len(part), CHUNK_LINES)] if n else [part]
        results = await asyncio.gather(*(one(c) for c in chunks))
        ok = ok and all(results)
        if n == 0 and not ok:
            return False  # даже ближайшие строки не вышли — Gemini недоступен, остальное не мучаем
    return ok


async def _check(lang: str, texts: list[str]) -> None:
    """Проверка с ВМ: cd /opt/lyrics-tv/backend && sudo ../venv/bin/python -m lyrics_tv.translit ja 突然のキスや"""
    print(f"модели: {', '.join(C.GEMINI_MODELS)} (+ {', '.join(FALLBACK_MODELS)}), "
          f"ключ: {'есть' if C.GEMINI_API_KEY else 'НЕ ЗАДАН'}")
    async with httpx.AsyncClient() as client:
        try:
            for t, r in zip(texts, await gemini(client, lang, texts)):
                print(f"{t} → {r}")
        except TranslitFailed as e:
            print(f"ОШИБКА: {e}")


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    lang = args.pop(0) if args and args[0] in SYSTEMS else "ja"
    asyncio.run(_check(lang, args or ["突然のキスや熱いまなざしで"]))
