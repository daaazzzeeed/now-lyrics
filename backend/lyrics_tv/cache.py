"""SQLite: кэш по Spotify track id (текст, транслитерация, палитра) и ручные поправки (overrides)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .lrc import migrate

VERSION = 3  # 3: защита от каверов NetEase, NetEase для азиатской музыки, romalrc → hint
STALE_VERSIONS = (2,)  # такие записи показываются сразу, а текст ищется заново в фоне
OVERRIDE_FIELDS = ("offset_ms", "source", "source_id", "lang")


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS track (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated INTEGER)")
        # key: "track:<spotify id>" или "artist:<имя>"; поля NULL — «не задано»
        self.db.execute("CREATE TABLE IF NOT EXISTS overrides (key TEXT PRIMARY KEY, offset_ms INTEGER, "
                        "source TEXT, source_id TEXT, lang TEXT, updated INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        self.db.commit()

    def get_kv(self, key: str):
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_kv(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    # ── кэш трека ──
    def get(self, tid: str) -> dict | None:
        row = self.db.execute("SELECT data FROM track WHERE id = ?", (tid,)).fetchone()
        data = json.loads(row[0]) if row else None
        if not isinstance(data, dict) or data.get("v") not in (VERSION, *STALE_VERSIONS):
            return None
        if data.get("v") != VERSION:
            data["stale"] = True  # показать сразу, но поискать текст заново по новым правилам
        migrate(data.get("lyrics"))
        if "romaji_done" in data:  # старое имя флага
            data.setdefault("translit_done", data.pop("romaji_done"))
        return data

    def put(self, tid: str, data: dict) -> None:
        data = dict(data, v=VERSION)
        data.pop("stale", None)
        self.db.execute("INSERT OR REPLACE INTO track (id, data, updated) VALUES (?, ?, ?)",
                        (tid, json.dumps(data, ensure_ascii=False), int(time.time())))
        self.db.commit()

    def update(self, tid: str, **fields) -> dict:
        data = self.get(tid) or {}
        data.update(fields)
        self.put(tid, data)
        return data

    def forget_lyrics(self, tid: str) -> None:
        """Сброс текста трека (после смены источника или языка) — палитра остаётся."""
        data = self.get(tid) or {}
        for k in ("lyrics", "checked", "translit_done", "meta"):
            data.pop(k, None)
        self.put(tid, data)

    # ── ручные поправки ──
    def override(self, tid: str, artists: list[str] | None = None) -> dict:
        """Поправки для трека: сначала по артисту, поверх — по треку."""
        out: dict = {}
        keys = [f"artist:{a.casefold()}" for a in (artists or [])] + [f"track:{tid}"]
        for key in keys:
            row = self.db.execute(f"SELECT {', '.join(OVERRIDE_FIELDS)} FROM overrides WHERE key = ?",
                                  (key,)).fetchone()
            if row:
                out.update({k: v for k, v in zip(OVERRIDE_FIELDS, row) if v is not None})
        return out

    def set_override(self, key: str, **fields) -> dict:
        """fields: значение None — удалить поле. Пустая запись удаляется целиком."""
        row = self.db.execute(f"SELECT {', '.join(OVERRIDE_FIELDS)} FROM overrides WHERE key = ?",
                              (key,)).fetchone()
        cur = dict(zip(OVERRIDE_FIELDS, row)) if row else dict.fromkeys(OVERRIDE_FIELDS)
        cur.update({k: v for k, v in fields.items() if k in OVERRIDE_FIELDS})
        if all(v is None for v in cur.values()):
            self.db.execute("DELETE FROM overrides WHERE key = ?", (key,))
        else:
            self.db.execute("INSERT OR REPLACE INTO overrides (key, offset_ms, source, source_id, lang, updated) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (key, cur["offset_ms"], cur["source"], cur["source_id"], cur["lang"], int(time.time())))
        self.db.commit()
        return {k: v for k, v in cur.items() if v is not None}
