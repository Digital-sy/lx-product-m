#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lx-product-m 配置读取。"""
from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)


@dataclass(frozen=True)
class Settings:
    lingxing_host: str = os.getenv("LINGXING_HOST", "https://openapi.lingxing.com").rstrip("/")
    lingxing_app_id: str = os.getenv("LINGXING_APP_ID", "")
    lingxing_app_secret: str = os.getenv("LINGXING_APP_SECRET", "")
    lingxing_proxy_url: str = os.getenv("LINGXING_PROXY_URL", "")

    db_host: str = os.getenv("DB_HOST", "")
    db_port: int = int(os.getenv("DB_PORT", "3306"))
    db_user: str = os.getenv("DB_USER", "")
    db_password: str = os.getenv("DB_PASSWORD", "")
    db_database: str = os.getenv("DB_DATABASE", "lingxing")
    db_charset: str = os.getenv("DB_CHARSET", "utf8mb4")

    feishu_app_id: str = os.getenv("FEISHU_APP_ID", "")
    feishu_app_secret: str = os.getenv("FEISHU_APP_SECRET", "")
    feishu_api_base: str = os.getenv("FEISHU_API_BASE", "https://open.feishu.cn/open-apis").rstrip("/")

    api_timeout_seconds: float = float(os.getenv("API_TIMEOUT_SECONDS", "60"))
    collection_delay_seconds: float = float(os.getenv("COLLECTION_DELAY_SECONDS", "1.2"))

    @property
    def db_config(self) -> dict:
        return {
            "host": self.db_host,
            "port": self.db_port,
            "user": self.db_user,
            "password": self.db_password,
            "database": self.db_database,
            "charset": self.db_charset,
            "connect_timeout": 10,
            "read_timeout": 600,
            "write_timeout": 600,
            "autocommit": False,
        }

    def validate_lingxing(self) -> None:
        missing = []
        if not self.lingxing_host:
            missing.append("LINGXING_HOST")
        if not self.lingxing_app_id:
            missing.append("LINGXING_APP_ID")
        if not self.lingxing_app_secret:
            missing.append("LINGXING_APP_SECRET")
        if missing:
            raise RuntimeError(f"领星配置缺失：{', '.join(missing)}")

    def validate_db(self) -> None:
        missing = []
        if not self.db_host:
            missing.append("DB_HOST")
        if not self.db_user:
            missing.append("DB_USER")
        if not self.db_password:
            missing.append("DB_PASSWORD")
        if not self.db_database:
            missing.append("DB_DATABASE")
        if missing:
            raise RuntimeError(f"数据库配置缺失：{', '.join(missing)}")

    def validate_feishu(self) -> None:
        missing = []
        if not self.feishu_app_id:
            missing.append("FEISHU_APP_ID")
        if not self.feishu_app_secret:
            missing.append("FEISHU_APP_SECRET")
        if missing:
            raise RuntimeError(f"飞书配置缺失：{', '.join(missing)}")

    def validate_all(self) -> None:
        self.validate_lingxing()
        self.validate_db()


settings = Settings()
