#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MySQL 访问封装。"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterable, Sequence

import pymysql
from pymysql.cursors import DictCursor

from .config import settings


def json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class Database:
    def __init__(self) -> None:
        settings.validate_db()
        self.config = settings.db_config
        self._conn = None

    def connect(self):
        """复用单个 MySQL 长连接，避免每次 SQL 都重新握手/认证。"""
        if self._conn is None:
            self._conn = pymysql.connect(**self.config, cursorclass=DictCursor)
        else:
            try:
                self._conn.ping(reconnect=True)
            except Exception:
                self._conn = pymysql.connect(**self.config, cursorclass=DictCursor)
        return self._conn

    @contextmanager
    def cursor(self):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> int:
        with self.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.rowcount

    def executemany(self, sql: str, params_seq: Sequence[Iterable[Any]], batch_size: int = 1000) -> int:
        if not params_seq:
            return 0
        total = 0
        with self.cursor() as cur:
            for i in range(0, len(params_seq), batch_size):
                batch = params_seq[i : i + batch_size]
                cur.executemany(sql, batch)
                total += cur.rowcount
        return total

    def fetch_one(self, sql: str, params: Iterable[Any] | None = None) -> dict | None:
        with self.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()

    def fetch_all(self, sql: str, params: Iterable[Any] | None = None) -> list[dict]:
        with self.cursor() as cur:
            cur.execute(sql, params or ())
            return list(cur.fetchall())
