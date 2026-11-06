"""Ромадзи для строк, которым не хватило romalrc: один запрос к Gemini REST на трек."""
from __future__ import annotations

import json
import logging

import httpx

from . import config as C
from .lrc import needs_romaji

log = logging.getLogger("romaji")

PROMPT = (
    "You transliterate Japanese song lyrics into romaji.\n"
    "Input: a JSON array of lyric lines in song order. Each item is an object: \"ja\" is the Japanese line, "
    "optional \"hint\" is a syllable-by-syllable reading of that line (its kanji readings are correct, "
    "but it is split into syllables, not words).\n"
    "Output: a JSON array of strings of exactly the same length; element i is the romaji of input line i.\n"
    "Rules: modified Hepburn; lowercase only; group syllables into words and separate words with single "
    "spaces (e.g. \"to tsu ze n no ki su ya\" -> \"totsuzen no kisu ya\"); particles は/へ/を as wa/e/o; "
    "long vowels as doubled letters (ou, uu, ee) without macrons; when there is no hint, read kanji the way "
    "they are sung, using the context of the whole song; keep Latin words and punctuation as they are; "
    "never translate; never merge or split lines."
)
FALLBACK_MODEL = "gemini-flash-latest"


class RomajiFailed(Exception):
    pass


async def gemini_romaji(client: httpx.AsyncClient, texts: list[str], hints: list[str | None] | None = None
                        ) -> list[str]:
    if not C.GEMINI_API_KEY:
        raise RomajiFailed("GEMINI_API_KEY не задан")
    hints = hints or [None] * len(texts)
    items = [{"ja": t, **({"hint": h} if h else {})} for t, h in zip(texts, hints)]
    body = {
        "systemInstruction": {"parts": [{"text": PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(items, ensure_ascii=False)}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
    }
    models = [C.GEMINI_MODEL] + ([FALLBACK_MODEL] if C.GEMINI_MODEL != FALLBACK_MODEL else [])
    for i, model in enumerate(models):
        url = f"{C.GEMINI_BASE}/v1beta/models/{model}:generateContent"
        try:
            r = await client.post(url, json=body, headers={"x-goog-api-key": C.GEMINI_API_KEY}, timeout=90)
        except httpx.HTTPError as e:
            raise RomajiFailed(f"сеть: {e!r}") from e
        if r.status_code == 404 and i + 1 < len(models):  # модель сняли — пробуем запасную
            log.warning("Gemini: модель %s недоступна (404), пробую %s", model, models[i + 1])
            continue
        if r.status_code != 200:
            raise RomajiFailed(f"{model}: HTTP {r.status_code}: {r.text[:300]}")
        break
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        out = json.loads("".join(p.get("text", "") for p in parts))
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise RomajiFailed(f"непонятный ответ: {e!r}: {r.text[:300]}") from e
    if not isinstance(out, list) or len(out) != len(texts):
        raise RomajiFailed(f"длина ответа {len(out) if isinstance(out, list) else '?'} ≠ {len(texts)}")
    return [s.strip().lower() if isinstance(s, str) else "" for s in out]


async def fill_romaji(client: httpx.AsyncClient, lyr: dict) -> bool:
    """Дописывает ромадзи в строки без него. True — работа закончена (кэшировать), False — повторить позже."""
    lines = lyr["lines"]
    todo = [i for i, ln in enumerate(lines) if needs_romaji(ln)]
    if not todo:
        return True
    try:
        out = await gemini_romaji(client, [lines[i]["text"] for i in todo],
                                  [lines[i].get("romaji") for i in todo])  # слоговый romalrc — как подсказка
    except RomajiFailed as e:
        log.warning("Gemini: %s", e)
        return False
    for i, r in zip(todo, out):
        if r:
            lines[i]["romaji"] = r
    log.info("Gemini: ромадзи для %d строк", len(todo))
    return True


async def _check(texts: list[str]) -> None:
    """Проверка с ВМ: sudo ../venv/bin/python -m lyrics_tv.romaji 突然のキスや熱いまなざしで"""
    print(f"модель: {C.GEMINI_MODEL}, ключ: {'есть' if C.GEMINI_API_KEY else 'НЕ ЗАДАН'}")
    async with httpx.AsyncClient() as client:
        try:
            for t, r in zip(texts, await gemini_romaji(client, texts)):
                print(f"{t} → {r}")
        except RomajiFailed as e:
            print(f"ОШИБКА: {e}")


if __name__ == "__main__":
    import asyncio
    import sys

    asyncio.run(_check(sys.argv[1:] or ["突然のキスや熱いまなざしで"]))
