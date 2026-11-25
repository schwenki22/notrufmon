"""Persistent result buffer (SQLite) so nothing is lost while the server is unreachable."""
from __future__ import annotations

import json
import os
import sqlite3

MAX_ROWS = 100_000


class Outbox:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT)")
        self.db.commit()

    def put(self, record: dict) -> None:
        self.db.execute("INSERT INTO outbox (payload) VALUES (?)", (json.dumps(record, default=str),))
        n = self.count()
        if n > MAX_ROWS:
            self.db.execute("DELETE FROM outbox WHERE id IN (SELECT id FROM outbox ORDER BY id LIMIT ?)",
                            (n - MAX_ROWS,))
        self.db.commit()

    def batch(self, limit: int = 200) -> list[tuple[int, dict]]:
        rows = self.db.execute("SELECT id, payload FROM outbox ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [(i, json.loads(p)) for i, p in rows]

    def delete(self, ids: list[int]) -> None:
        if ids:
            self.db.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])
            self.db.commit()

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
