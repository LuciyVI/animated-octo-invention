from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from bot.config import Settings
from bot.db import Database
from bot.services import BotService, ServiceError
from tests.fakes import FakeXUIService, make_reality_inbound


def _create_db_with_legacy_configs_fk(path: str) -> None:
    now = int(time.time())
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE users (
                tg_id INTEGER PRIMARY KEY,
                inbound_id INTEGER UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                expires_at INTEGER,
                max_configs INTEGER NOT NULL DEFAULT 5,
                created_by TEXT NOT NULL DEFAULT 'manual',
                access_source TEXT NOT NULL DEFAULT 'admin',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                inbound_id INTEGER NOT NULL,
                client_uuid TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                share_link TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(tg_id) REFERENCES "users_legacy_notnull"(tg_id)
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_tg_id INTEGER,
                target_tg_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE port_allocations (
                port INTEGER PRIMARY KEY,
                tg_id INTEGER UNIQUE NOT NULL,
                inbound_id INTEGER UNIQUE,
                status TEXT NOT NULL DEFAULT 'reserved',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE provisioning_locks (
                tg_id INTEGER PRIMARY KEY,
                locked_at INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO users (
                tg_id, inbound_id, status, expires_at, max_configs,
                created_by, access_source, created_at, updated_at
            )
            VALUES (123456789, 7, 'active', NULL, 5, 'auto_group', 'telegram_group', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO configs (
                id, tg_id, inbound_id, client_uuid, email, title,
                share_link, enabled, created_at, updated_at
            )
            VALUES (1, 123456789, 7, 'uuid-1', 'tg_123456789_01', 'phone', NULL, 1, ?, ?)
            """,
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _fk_target(path: str) -> tuple[str, str, str]:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("PRAGMA foreign_key_list(configs)").fetchone()
        assert row is not None
        return str(row[2]), str(row[3]), str(row[4])
    finally:
        conn.close()


def test_configs_fk_migration_repairs_legacy_users_table_reference(tmp_path):
    path = str(tmp_path / "bot.db")
    _create_db_with_legacy_configs_fk(path)

    db = Database(path)
    db.init()
    try:
        assert db.configs_foreign_key_valid() is True
        assert db.count_configs(123456789) == 1
    finally:
        db.close()

    assert _fk_target(path) == ("users", "tg_id", "tg_id")


def test_create_config_refuses_xui_mutation_when_configs_fk_invalid(tmp_path):
    path = str(tmp_path / "bot.db")
    _create_db_with_legacy_configs_fk(path)
    db = Database(path)
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    settings = Settings(
        bot_token="telegram-token",
        admin_ids={111111111},
        xui_host="https://panel.example.com/",
        xui_token="xui-token",
        public_host="vpn.example.com",
        db_path=path,
    )
    service = BotService(db, fake_xui, settings)

    try:
        with pytest.raises(ServiceError, match="Локальная БД требует миграции"):
            asyncio.run(service.create_config(123456789, "phone"))
        assert fake_xui.add_client_calls == 0
    finally:
        db.close()
