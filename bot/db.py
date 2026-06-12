from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from bot.models import ConfigRecord, UserRecord


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    inbound_id INTEGER UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    expires_at INTEGER,
    max_configs INTEGER NOT NULL DEFAULT 5,
    created_by TEXT NOT NULL DEFAULT 'auto_group',
    access_source TEXT NOT NULL DEFAULT 'telegram_group',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS configs (
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
    FOREIGN KEY(tg_id) REFERENCES users(tg_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_tg_id INTEGER,
    target_tg_id INTEGER,
    action TEXT NOT NULL,
    details TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS port_allocations (
    port INTEGER PRIMARY KEY,
    tg_id INTEGER UNIQUE NOT NULL,
    inbound_id INTEGER UNIQUE,
    status TEXT NOT NULL DEFAULT 'reserved',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS provisioning_locks (
    tg_id INTEGER PRIMARY KEY,
    locked_at INTEGER NOT NULL
);
"""


CONFIG_COLUMNS = (
    "id",
    "tg_id",
    "inbound_id",
    "client_uuid",
    "email",
    "title",
    "share_link",
    "enabled",
    "created_at",
    "updated_at",
)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()

    def init(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _table_columns(self, table: str) -> dict[str, sqlite3.Row]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]): row for row in rows}

    def _migrate(self) -> None:
        columns = self._table_columns("users")
        if "inbound_id" in columns and int(columns["inbound_id"]["notnull"]):
            self._conn.executescript(
                """
                PRAGMA foreign_keys = OFF;
                ALTER TABLE users RENAME TO users_legacy_notnull;
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
                INSERT INTO users (
                    tg_id, inbound_id, status, expires_at, max_configs,
                    created_by, access_source, created_at, updated_at
                )
                SELECT
                    tg_id, inbound_id, status, expires_at, max_configs,
                    'manual', 'admin', created_at, updated_at
                FROM users_legacy_notnull;
                DROP TABLE users_legacy_notnull;
                PRAGMA foreign_keys = ON;
                """
            )
            columns = self._table_columns("users")
        if "created_by" not in columns:
            self._conn.execute("ALTER TABLE users ADD COLUMN created_by TEXT NOT NULL DEFAULT 'manual'")
        if "access_source" not in columns:
            self._conn.execute("ALTER TABLE users ADD COLUMN access_source TEXT NOT NULL DEFAULT 'admin'")
        self._migrate_configs_foreign_key()

    def _config_extra_column_definition(self, row: sqlite3.Row) -> str:
        parts = [_quote_identifier(str(row["name"]))]
        column_type = str(row["type"] or "").strip()
        if column_type:
            parts.append(column_type)
        if int(row["notnull"]):
            parts.append("NOT NULL")
        default = row["dflt_value"]
        if default is not None:
            parts.append(f"DEFAULT {default}")
        return " ".join(parts)

    def _configs_foreign_key_points_to_users(self) -> bool:
        rows = self._conn.execute("PRAGMA foreign_key_list(configs)").fetchall()
        return any(
            str(row["table"]) == "users"
            and str(row["from"]) == "tg_id"
            and str(row["to"]) == "tg_id"
            for row in rows
        )

    def configs_foreign_key_valid(self) -> bool:
        with self._lock:
            return self._configs_foreign_key_points_to_users()

    def assert_configs_schema_valid(self) -> None:
        with self._lock:
            if not self._configs_foreign_key_points_to_users():
                raise RuntimeError("configs.tg_id foreign key must reference users(tg_id)")

    def _migrate_configs_foreign_key(self) -> None:
        if self._configs_foreign_key_points_to_users():
            return

        columns = self._table_columns("configs")
        missing_columns = [name for name in CONFIG_COLUMNS if name not in columns]
        if missing_columns:
            raise RuntimeError(f"configs table is missing required columns: {', '.join(missing_columns)}")

        extra_columns = [
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(configs)").fetchall()
            if str(row["name"]) not in CONFIG_COLUMNS
        ]
        extra_definitions = [
            self._config_extra_column_definition(columns[name])
            for name in extra_columns
        ]
        all_columns = [*CONFIG_COLUMNS, *extra_columns]
        quoted_columns = ", ".join(_quote_identifier(name) for name in all_columns)
        create_columns = [
            '"id" INTEGER PRIMARY KEY AUTOINCREMENT',
            '"tg_id" INTEGER NOT NULL',
            '"inbound_id" INTEGER NOT NULL',
            '"client_uuid" TEXT NOT NULL UNIQUE',
            '"email" TEXT NOT NULL UNIQUE',
            '"title" TEXT NOT NULL',
            '"share_link" TEXT',
            '"enabled" INTEGER NOT NULL DEFAULT 1',
            '"created_at" INTEGER NOT NULL',
            '"updated_at" INTEGER NOT NULL',
            *extra_definitions,
            'FOREIGN KEY("tg_id") REFERENCES "users"("tg_id")',
        ]

        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self._conn.execute("DROP TABLE IF EXISTS configs_new")
            self._conn.execute(
                "CREATE TABLE configs_new (\n"
                + ",\n".join(f"    {definition}" for definition in create_columns)
                + "\n)"
            )
            self._conn.execute(
                f"INSERT INTO configs_new ({quoted_columns}) "
                f"SELECT {quoted_columns} FROM configs"
            )
            self._conn.execute("DROP TABLE configs")
            self._conn.execute("ALTER TABLE configs_new RENAME TO configs")
        finally:
            self._conn.execute("PRAGMA foreign_keys = ON")

        if not self._configs_foreign_key_points_to_users():
            raise RuntimeError("failed to migrate configs foreign key to users(tg_id)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _row_to_user(self, row: sqlite3.Row | None) -> UserRecord | None:
        if row is None:
            return None
        return UserRecord(
            tg_id=int(row["tg_id"]),
            inbound_id=None if row["inbound_id"] is None else int(row["inbound_id"]),
            status=str(row["status"]),
            expires_at=None if row["expires_at"] is None else int(row["expires_at"]),
            max_configs=int(row["max_configs"]),
            created_by=str(row["created_by"]),
            access_source=str(row["access_source"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def _row_to_config(self, row: sqlite3.Row | None) -> ConfigRecord | None:
        if row is None:
            return None
        return ConfigRecord(
            id=int(row["id"]),
            tg_id=int(row["tg_id"]),
            inbound_id=int(row["inbound_id"]),
            client_uuid=str(row["client_uuid"]),
            email=str(row["email"]),
            title=str(row["title"]),
            share_link=None if row["share_link"] is None else str(row["share_link"]),
            enabled=bool(row["enabled"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def get_user(self, tg_id: int) -> UserRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
            return self._row_to_user(row)

    def get_user_by_inbound(self, inbound_id: int) -> UserRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE inbound_id = ?",
                (inbound_id,),
            ).fetchone()
            return self._row_to_user(row)

    def upsert_user(
        self,
        tg_id: int,
        inbound_id: int,
        status: str,
        expires_at: int | None,
        max_configs: int,
        created_by: str = "manual",
        access_source: str = "admin",
    ) -> UserRecord:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO users (
                    tg_id, inbound_id, status, expires_at, max_configs,
                    created_by, access_source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tg_id) DO UPDATE SET
                    inbound_id = excluded.inbound_id,
                    status = excluded.status,
                    expires_at = excluded.expires_at,
                    max_configs = excluded.max_configs,
                    created_by = excluded.created_by,
                    access_source = excluded.access_source,
                    updated_at = excluded.updated_at
                """,
                (tg_id, inbound_id, status, expires_at, max_configs, created_by, access_source, now, now),
            )
            user = self.get_user(tg_id)
            if user is None:
                raise RuntimeError("failed to upsert user")
            return user

    def create_pending_user(
        self,
        tg_id: int,
        max_configs: int,
        created_by: str = "auto_group",
        access_source: str = "telegram_group",
    ) -> UserRecord:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO users (
                    tg_id, inbound_id, status, expires_at, max_configs,
                    created_by, access_source, created_at, updated_at
                )
                VALUES (?, NULL, 'pending', NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(tg_id) DO NOTHING
                """,
                (tg_id, max_configs, created_by, access_source, now, now),
            )
            user = self.get_user(tg_id)
            if user is None:
                raise RuntimeError("failed to create pending user")
            return user

    def set_user_status(self, tg_id: int, status: str) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE users SET status = ?, updated_at = ? WHERE tg_id = ?",
                (status, now, tg_id),
            )

    def set_user_expires_at(self, tg_id: int, expires_at: int | None) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE users SET expires_at = ?, updated_at = ? WHERE tg_id = ?",
                (expires_at, now, tg_id),
            )

    def set_user_status_and_expiry(
        self,
        tg_id: int,
        status: str,
        expires_at: int | None,
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE users SET status = ?, expires_at = ?, updated_at = ? WHERE tg_id = ?",
                (status, expires_at, now, tg_id),
            )

    def list_users(self) -> list[UserRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM users ORDER BY tg_id").fetchall()
            return [user for row in rows if (user := self._row_to_user(row)) is not None]

    def count_auto_users(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM users WHERE created_by = 'auto_group' AND inbound_id IS NOT NULL"
                ).fetchone()[0]
            )

    def allocated_ports(self, active_only: bool = False) -> set[int]:
        query = "SELECT port FROM port_allocations"
        if active_only:
            query += " WHERE status IN ('reserved', 'active')"
        with self._lock:
            rows = self._conn.execute(query).fetchall()
            return {int(row["port"]) for row in rows}

    def reserve_port(self, port: int, tg_id: int) -> bool:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "DELETE FROM port_allocations WHERE tg_id = ? AND status = 'failed'",
                (tg_id,),
            )
            try:
                self._conn.execute(
                    """
                    INSERT INTO port_allocations (port, tg_id, inbound_id, status, created_at, updated_at)
                    VALUES (?, ?, NULL, 'reserved', ?, ?)
                    """,
                    (port, tg_id, now, now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def set_port_allocation(self, port: int, status: str, inbound_id: int | None = None) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                UPDATE port_allocations
                SET status = ?, inbound_id = ?, updated_at = ?
                WHERE port = ?
                """,
                (status, inbound_id, now, port),
            )

    def acquire_provisioning_lock(self, tg_id: int) -> bool:
        now = int(time.time())
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO provisioning_locks (tg_id, locked_at) VALUES (?, ?)",
                    (tg_id, now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def release_provisioning_lock(self, tg_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM provisioning_locks WHERE tg_id = ?", (tg_id,))

    def list_expired_active_users(self, now_ts: int) -> list[UserRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM users
                WHERE status = 'active'
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                ORDER BY expires_at
                """,
                (now_ts,),
            ).fetchall()
            return [user for row in rows if (user := self._row_to_user(row)) is not None]

    def add_config(
        self,
        tg_id: int,
        inbound_id: int,
        client_uuid: str,
        email: str,
        title: str,
        share_link: str | None,
    ) -> ConfigRecord:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO configs (
                    tg_id, inbound_id, client_uuid, email, title, share_link,
                    enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (tg_id, inbound_id, client_uuid, email, title, share_link, now, now),
            )
            config = self.get_config_by_id(int(cur.lastrowid))
            if config is None:
                raise RuntimeError("failed to insert config")
            return config

    def get_config_by_id(self, config_id: int) -> ConfigRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM configs WHERE id = ?", (config_id,)).fetchone()
            return self._row_to_config(row)

    def get_config_by_number(self, tg_id: int, number: int) -> ConfigRecord | None:
        if number < 1:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM configs
                WHERE tg_id = ?
                ORDER BY id
                LIMIT 1 OFFSET ?
                """,
                (tg_id, number - 1),
            ).fetchone()
            return self._row_to_config(row)

    def list_configs(self, tg_id: int) -> list[ConfigRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM configs WHERE tg_id = ? ORDER BY id",
                (tg_id,),
            ).fetchall()
            return [cfg for row in rows if (cfg := self._row_to_config(row)) is not None]

    def list_configs_by_inbound(self, inbound_id: int, enabled_only: bool = False) -> list[ConfigRecord]:
        query = "SELECT * FROM configs WHERE inbound_id = ?"
        params: list[Any] = [inbound_id]
        if enabled_only:
            query += " AND enabled = 1"
        query += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
            return [cfg for row in rows if (cfg := self._row_to_config(row)) is not None]

    def count_configs(self, tg_id: int, enabled_only: bool = False) -> int:
        query = "SELECT COUNT(*) FROM configs WHERE tg_id = ?"
        params: list[Any] = [tg_id]
        if enabled_only:
            query += " AND enabled = 1"
        with self._lock:
            return int(self._conn.execute(query, params).fetchone()[0])

    def count_enabled_configs_for_inbound(self, inbound_id: int) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM configs WHERE inbound_id = ? AND enabled = 1",
                    (inbound_id,),
                ).fetchone()[0]
            )

    def next_config_index(self, tg_id: int) -> int:
        prefix = f"tg_{tg_id}_"
        max_index = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT email FROM configs WHERE tg_id = ?",
                (tg_id,),
            ).fetchall()
        for row in rows:
            email = str(row["email"])
            if not email.startswith(prefix):
                continue
            suffix = email[len(prefix):]
            if suffix.isdigit():
                max_index = max(max_index, int(suffix))
        return max_index + 1

    def set_config_enabled(self, config_id: int, enabled: bool) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE configs SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, now, config_id),
            )

    def disable_user_configs(self, tg_id: int) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE configs SET enabled = 0, updated_at = ? WHERE tg_id = ?",
                (now, tg_id),
            )

    def add_audit(
        self,
        actor_tg_id: int | None,
        target_tg_id: int | None,
        action: str,
        details: str | None = None,
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_log (actor_tg_id, target_tg_id, action, details, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (actor_tg_id, target_tg_id, action, details, now),
            )

    def audit_entries(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
