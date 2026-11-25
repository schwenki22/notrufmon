"""Database access (psycopg 3, synchronous; called from threads)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import threading

import psycopg

log = logging.getLogger("notrufmon.db")
SCHEMA = pathlib.Path(__file__).with_name("schema.sql")


def _ts(v) -> dt.datetime:
    if v is None:
        return dt.datetime.now(dt.timezone.utc)
    return dt.datetime.fromtimestamp(float(v), dt.timezone.utc)


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn: psycopg.Connection | None = None
        self._lock = threading.Lock()

    def _connect(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=10)
        return self._conn

    def execute(self, sql: str, params=None) -> None:
        with self._lock:
            for attempt in range(2):
                try:
                    with self._connect().cursor() as cur:
                        cur.execute(sql, params)
                    return
                except psycopg.OperationalError:
                    self._conn = None
                    if attempt:
                        raise

    def transaction(self, statements: list[tuple[str, tuple]]) -> None:
        """Run several statements atomically (all or nothing)."""
        with self._lock:
            conn = self._connect()
            with conn.transaction():
                with conn.cursor() as cur:
                    for sql, params in statements:
                        cur.execute(sql, params)

    def query(self, sql: str, params=None) -> list[dict]:
        """Run a statement and return all rows as dicts (empty list if no result set)."""
        from psycopg.rows import dict_row
        with self._lock:
            for attempt in range(2):
                try:
                    with self._connect().cursor(row_factory=dict_row) as cur:
                        cur.execute(sql, params)
                        return cur.fetchall() if cur.description else []
                except psycopg.OperationalError:
                    self._conn = None
                    if attempt:
                        raise
        return []

    def ensure_schema(self) -> None:
        self.execute(SCHEMA.read_text())
