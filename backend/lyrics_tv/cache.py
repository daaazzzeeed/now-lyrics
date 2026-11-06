"""SQLite-кэш по Spotify track id: текст, ромадзи, палитра, время последней проверки."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

VERSION = 1


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS track (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated INTEGER)")
        self.db.commit()

    def get(self, tid: str) -> dict | None:
        row = self.db.execute("SELECT data FROM track WHERE id = ?", (tid,)).fetchone()
        data = json.loads(row[0]) if row else None
        return data if isinstance(data, dict) and data.get("v") == VERSION else None

    def put(self, tid: str, data: dict) -> None:
        data = dict(data, v=VERSION)
        self.db.execute("INSERT OR REPLACE INTO track (id, data, updated) VALUES (?, ?, ?)",
                        (tid, json.dumps(data, ensure_ascii=False), int(time.time())))
        self.db.commit()

    def update(self, tid: str, **fields) -> dict:
        data = self.get(tid) or {}
        data.update(fields)
        self.put(tid, data)
        return data
