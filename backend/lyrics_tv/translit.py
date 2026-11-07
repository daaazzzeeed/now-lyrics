"""Транслитерация строк: ромадзи, корейская романизация, пиньинь, Jyutping.

Сначала локальные библиотеки в подпроцессе (translit_worker, ~1 с на трек, без сети).
Gemini — только для строк, которые библиотеки не осилили (нет библиотеки, сбой).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Callable

import httpx

from . import config as C
from .lrc import HANGUL, KANA, is_syllabic, needs_translit, norm

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


WORKER_TIMEOUT_S = 60


async def run_worker(han: str, texts: list[str], hints: list[str | None]) -> list[str | None]:
    """Локальные библиотеки в подпроцессе: словари (до ~150 МБ у pykakasi) не живут в памяти сервера."""
    task = {"han": han, "items": [{"text": t, "hint": h} for t, h in zip(texts, hints)]}
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "lyrics_tv.translit_worker", cwd=str(C.BASE),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(task, ensure_ascii=False).encode()),
                                          WORKER_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError) as e:
        log.warning("локальная транслитерация: %r", e)
        return [None] * len(texts)
    if proc.returncode != 0:
        log.warning("локальная транслитерация: %s", err.decode(errors="replace").strip()[-300:])
        return [None] * len(texts)
    if err.strip():
        log.info("локальная транслитерация: %s", err.decode(errors="replace").strip()[-300:])
    try:
        res = json.loads(out)
    except ValueError:
        return [None] * len(texts)
    return res if isinstance(res, list) and len(res) == len(texts) else [None] * len(texts)


def apply_known(lines: list[dict], known: dict[str, str]) -> int:
    """Дописывает транслитерацию из уже полученных ответов (по нормализованному тексту строки)."""
    n = 0
    for ln in lines:
        if needs_translit(ln):
            tr = known.get(norm(ln["text"]))
            if tr:
                ln["translit"] = tr
                n += 1
    return n


def _gemini_lang(text: str, han: str) -> str:
    if KANA.search(text):
        return "ja"
    if HANGUL.search(text):
        return "ko"
    return han


async def fill(client: httpx.AsyncClient, lyr: dict, han: str, known: dict[str, str],
               on_update: Callable[[], None] | None = None) -> bool:
    """Транслитерация недостающих строк. True — всё готово (можно кэшировать)."""
    lines = lyr["lines"]
    if apply_known(lines, known) and on_update:
        on_update()
    todo = [i for i, ln in enumerate(lines) if needs_translit(ln)]
    if not todo:
        return True
    t0 = time.monotonic()
    hints = [lines[i].get("translit") if lines[i].get("translit") and is_syllabic(lines[i]["translit"]) else None
             for i in todo]  # слоговый romalrc NetEase — источник певческих чтений
    out = await run_worker(han, [lines[i]["text"] for i in todo], hints)
    done = 0
    for i, r in zip(todo, out):
        if r:
            lines[i]["translit"] = r
            known[norm(lines[i]["text"])] = r
            done += 1
    log.info("локальная транслитерация (%s): %d из %d строк за %d мс", han, done, len(todo),
             (time.monotonic() - t0) * 1000)
    if done and on_update:
        on_update()
    rest = [i for i, r in zip(todo, out) if not r]
    if not rest:
        return True
    if not C.GEMINI_API_KEY:
        return False
    # Gemini — только запасной вариант, кусками по CHUNK_LINES, по языку строки
    groups: dict[str, list[int]] = {}
    for i in rest:
        groups.setdefault(_gemini_lang(lines[i]["text"], han), []).append(i)
    sem = asyncio.Semaphore(PARALLEL)

    async def one(lang: str, part: list[int]) -> bool:
        async with sem:
            try:
                res = await gemini(client, lang, [lines[i]["text"] for i in part],
                                   [lines[i].get("translit") for i in part])
            except TranslitFailed as e:
                log.warning("Gemini (%s): %s", lang, e)
                return False
        for i, r in zip(part, res):
            if r:
                lines[i]["translit"] = r
                known[norm(lines[i]["text"])] = r
        if on_update:
            on_update()
        return all(res)

    jobs = [one(lang, idx[k:k + CHUNK_LINES]) for lang, idx in groups.items()
            for k in range(0, len(idx), CHUNK_LINES)]
    return all(await asyncio.gather(*jobs))


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
