#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MySQL 访问封装。"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterable

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

    def connect(self):
        return pymysql.connect(**self.config, cursorclass=DictCursor)

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
        finally:
            conn.close()

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> int:
        with self.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.rowcount

    def fetch_one(self, sql: str, params: Iterable[Any] | None = None) -> dict | None:
        with self.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()

    def fetch_all(self, sql: str, params: Iterable[Any] | None = None) -> list[dict]:
        with self.cursor() as cur:
            cur.execute(sql, params or ())
            return list(cur.fetchall())
