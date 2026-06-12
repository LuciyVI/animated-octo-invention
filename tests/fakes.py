from __future__ import annotations

import copy
import json
from typing import Any

from bot.config import Settings
from bot.db import Database
from bot.services import BotService
from bot.xui_client import (
    build_inbound_payload_from_template,
    client_belongs_to_tg,
    client_is_enabled,
    count_active_clients_in_inbound,
    extract_clients,
    extract_settings,
)


ADMIN_ID = 111111111


def make_settings(db_path: str, admin_ids: set[int] | None = None) -> Settings:
    return Settings(
        bot_token="telegram-token",
        admin_ids=admin_ids or {ADMIN_ID},
        xui_host="https://panel.example.com/secret-path/",
        xui_token="xui-token",
        public_host="vpn.example.com",
        max_configs_per_inbound=5,
        default_client_days=30,
        default_client_traffic_gb=0,
        db_path=db_path,
    )


def make_db(path: str) -> Database:
    db = Database(path)
    db.init()
    return db


def make_service(path: str, fake_xui: "FakeXUIService", admin_ids: set[int] | None = None) -> BotService:
    settings = make_settings(path, admin_ids)
    db = make_db(path)
    return BotService(db, fake_xui, settings)


def make_reality_inbound(
    inbound_id: int = 7,
    clients: list[dict[str, Any]] | None = None,
    protocol: str = "vless",
) -> dict[str, Any]:
    return {
        "id": inbound_id,
        "remark": f"inbound-{inbound_id}",
        "port": 443,
        "protocol": protocol,
        "enable": True,
        "settings": {
            "clients": clients or [],
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "serverNames": ["example.com"],
                "shortIds": ["abcdef"],
                "settings": {
                    "publicKey": "PUBLIC_KEY",
                    "fingerprint": "chrome",
                },
            },
        },
    }


def make_client(
    client_id: str,
    email: str,
    enabled: bool = True,
    tg_id: int | None = None,
) -> dict[str, Any]:
    client = {
        "id": client_id,
        "email": email,
        "flow": "",
        "enable": enabled,
    }
    if tg_id is not None:
        client["tgId"] = int(tg_id)
    return client


class FakeXUIService:
    def __init__(self, inbounds: list[dict[str, Any]]) -> None:
        self.inbounds = {int(inbound["id"]): copy.deepcopy(inbound) for inbound in inbounds}
        self.add_client_calls = 0
        self.disable_client_calls = 0
        self.mutate_calls: list[str] = []

    def snapshot(self) -> dict[int, dict[str, Any]]:
        return copy.deepcopy(self.inbounds)

    async def healthcheck(self) -> bool:
        return True

    async def list_inbounds(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self.inbounds.values()))

    async def get_inbound(self, inbound_id: int) -> dict[str, Any] | None:
        inbound = self.inbounds.get(inbound_id)
        return None if inbound is None else copy.deepcopy(inbound)

    async def count_active_clients(self, inbound_id: int) -> int:
        inbound = self.inbounds.get(inbound_id)
        if inbound is None:
            return 0
        return count_active_clients_in_inbound(inbound)

    async def add_client(
        self,
        inbound_id: int,
        telegram_id: int,
        title: str,
        client_uuid: str,
        email: str,
        expires_at: int | None,
        traffic_gb: int,
    ) -> None:
        self.add_client_calls += 1
        self.mutate_calls.append("addClient")
        inbound = self.inbounds[inbound_id]
        settings = extract_settings(inbound)
        clients = settings.setdefault("clients", [])
        clients.append(
            {
                "id": client_uuid,
                "email": email,
                "flow": "",
                "enable": True,
                "tgId": int(telegram_id),
                "comment": title,
                "expiryTime": 0 if expires_at is None else expires_at * 1000,
                "totalGB": traffic_gb,
            }
        )
        if isinstance(inbound.get("settings"), str):
            inbound["settings"] = json.dumps(settings)
        else:
            inbound["settings"] = settings

    async def create_inbound_from_template(
        self,
        template_inbound_id: int,
        port: int,
        remark: str,
    ) -> dict[str, Any]:
        self.mutate_calls.append("addInbound")
        template = self.inbounds[template_inbound_id]
        payload = build_inbound_payload_from_template(template, port=port, remark=remark)
        new_id = max(self.inbounds) + 1 if self.inbounds else 1
        created = {
            "id": new_id,
            "remark": payload["remark"],
            "port": payload["port"],
            "protocol": payload["protocol"],
            "enable": payload["enable"],
            "listen": payload["listen"],
            "expiryTime": payload["expiryTime"],
            "total": payload["total"],
            "settings": payload["settings"],
            "streamSettings": payload["streamSettings"],
            "sniffing": payload["sniffing"],
            "tag": f"in-{payload['port']}",
        }
        self.inbounds[new_id] = created
        return copy.deepcopy(created)

    async def update_inbound(self, inbound_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
        self.mutate_calls.append("updateInbound")
        inbound = self.inbounds.get(inbound_id)
        if inbound is None:
            return None
        updated = copy.deepcopy(inbound)
        for key, value in payload.items():
            if key == "id":
                continue
            updated[key] = copy.deepcopy(value)
        updated["id"] = inbound_id
        self.inbounds[inbound_id] = updated
        return copy.deepcopy(updated)

    async def get_inbound_by_remark_exact(self, remark: str) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(inbound)
            for inbound in self.inbounds.values()
            if str(inbound.get("remark") or "") == remark
        ]

    async def find_template_inbound(
        self,
        template_inbound_id: int | None = None,
        template_remark: str = "Moroz",
    ) -> dict[str, Any]:
        if template_inbound_id is not None:
            inbound = self.inbounds.get(template_inbound_id)
            if inbound is None:
                raise RuntimeError(f"template inbound {template_inbound_id} not found")
            return copy.deepcopy(inbound)
        matches = await self.get_inbound_by_remark_exact(template_remark)
        if not matches:
            raise RuntimeError(f"template inbound with remark {template_remark!r} not found")
        if len(matches) > 1:
            raise RuntimeError(f"multiple template inbounds with remark {template_remark!r}")
        return matches[0]

    async def create_inbound_from_template_inbound(
        self,
        template_inbound: dict[str, Any],
        telegram_id: int,
        port: int,
    ) -> int:
        self.mutate_calls.append("addInbound")
        payload = build_inbound_payload_from_template(
            template_inbound,
            port=port,
            remark=f"tg_{telegram_id}",
            tag=f"tg_{telegram_id}",
        )
        payload["enable"] = True
        new_id = max(self.inbounds) + 1 if self.inbounds else 1
        created = {
            "id": new_id,
            "remark": payload["remark"],
            "port": payload["port"],
            "protocol": payload["protocol"],
            "enable": payload["enable"],
            "listen": payload["listen"],
            "expiryTime": payload["expiryTime"],
            "total": payload["total"],
            "settings": payload["settings"],
            "streamSettings": payload["streamSettings"],
            "sniffing": payload["sniffing"],
            "tag": payload.get("tag", f"in-{payload['port']}"),
        }
        self.inbounds[new_id] = created
        return new_id

    async def verify_created_inbound(
        self,
        inbound_id: int,
        template_inbound: dict[str, Any],
        telegram_id: int,
        port: int,
    ) -> dict[str, Any]:
        created = self.inbounds.get(inbound_id)
        if created is None:
            return {"ok": False, "errors": ["created inbound not found"], "inbound": None}
        errors: list[str] = []
        if str(created.get("remark") or "") != f"tg_{telegram_id}":
            errors.append("remark mismatch")
        if int(created.get("port") or 0) != port:
            errors.append("port mismatch")
        if created.get("protocol") != template_inbound.get("protocol"):
            errors.append("protocol mismatch")
        if extract_clients(created):
            errors.append("clients not empty")
        if created.get("streamSettings") != template_inbound.get("streamSettings"):
            errors.append("streamSettings mismatch")
        if created.get("sniffing", {}) != template_inbound.get("sniffing", {}):
            errors.append("sniffing mismatch")
        return {"ok": not errors, "errors": errors, "inbound": copy.deepcopy(created)}

    async def disable_client(self, inbound_id: int, client_uuid: str) -> None:
        self.disable_client_calls += 1
        self.mutate_calls.append("updateClient")
        inbound = self.inbounds[inbound_id]
        settings = extract_settings(inbound)
        for client in settings.get("clients", []):
            if str(client.get("id")) == client_uuid:
                client["enable"] = False
        if isinstance(inbound.get("settings"), str):
            inbound["settings"] = json.dumps(settings)
        else:
            inbound["settings"] = settings

    async def disable_user_clients(self, inbound_id: int, telegram_id: int) -> None:
        inbound = self.inbounds[inbound_id]
        settings = extract_settings(inbound)
        for client in settings.get("clients", []):
            if client_belongs_to_tg(client, telegram_id) and client_is_enabled(client):
                client["enable"] = False
        if isinstance(inbound.get("settings"), str):
            inbound["settings"] = json.dumps(settings)
        else:
            inbound["settings"] = settings
