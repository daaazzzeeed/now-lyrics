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
    "Input: a JSON array of lyric lines in song order. Output: a JSON array of strings of exactly the same "
    "length, element i is the romaji of input line i.\n"
    "Rules: modified Hepburn; lowercase only; separate words with single spaces; particles は/へ/を as "
    "wa/e/o; long vowels as doubled letters (ou, uu, ee) without macrons; read kanji the way they are sung, "
    "using the context of the whole song; keep Latin words and punctuation as they are; never translate; "
    "never merge or split lines."
)


class RomajiFailed(Exception):
    pass


async def gemini_romaji(client: httpx.AsyncClient, texts: list[str]) -> list[str]:
    if not C.GEMINI_API_KEY:
        raise RomajiFailed("GEMINI_API_KEY не задан")
    body = {
        "systemInstruction": {"parts": [{"text": PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(texts, ensure_ascii=False)}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
    }
    url = f"{C.GEMINI_BASE}/v1beta/models/{C.GEMINI_MODEL}:generateContent"
    try:
        r = await client.post(url, json=body, headers={"x-goog-api-key": C.GEMINI_API_KEY}, timeout=60)
    except httpx.HTTPError as e:
        raise RomajiFailed(f"сеть: {e}") from e
    if r.status_code != 200:
        raise RomajiFailed(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        out = json.loads("".join(p.get("text", "") for p in parts))
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise RomajiFailed(f"непонятный ответ: {e}") from e
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
        out = await gemini_romaji(client, [lines[i]["text"] for i in todo])
    except RomajiFailed as e:
        log.warning("Gemini: %s", e)
        return False
    for i, r in zip(todo, out):
        if r:
            lines[i]["romaji"] = r
    return True
