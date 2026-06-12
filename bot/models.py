from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UserRecord:
    tg_id: int
    inbound_id: int | None
    status: str
    expires_at: int | None
    max_configs: int
    created_by: str
    access_source: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class ConfigRecord:
    id: int
    tg_id: int
    inbound_id: int
    client_uuid: str
    email: str
    title: str
    share_link: str | None
    enabled: bool
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class CreatedConfig:
    config: ConfigRecord
    share_link: str
    immutable_changes: list[str] | None = None
    client_integrity_warnings: list[str] | None = None


@dataclass(frozen=True)
class SyncReport:
    checked: int
    orphaned: list[int]
    existing: list[int]
