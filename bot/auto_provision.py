from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from bot.access_control import AccessControlService
from bot.config import Settings
from bot.db import Database
from bot.link_builder import LinkBuildError, build_share_link
from bot.xui_client import (
    compare_inbound_immutable_fields,
    count_active_clients_in_inbound,
    extract_clients,
)


@dataclass(frozen=True)
class ProvisionResult:
    allowed: bool
    status: str
    created: bool = False
    inbound_id: int | None = None
    port: int | None = None
    message: str = ""
    reason: str | None = None


@dataclass(frozen=True)
class TemplateCheckResult:
    ok: bool
    found: bool
    reason: str
    inbound: dict[str, Any] | None
    link_supported: bool
    link_reason: str


@dataclass(frozen=True)
class AutoProvisionStatus:
    enabled: bool
    template_remark: str
    port_min: int
    port_max: int
    used_ports: int
    free_ports: int
    auto_users: int


class AutoProvisionService:
    def __init__(
        self,
        db: Database,
        xui: object,
        settings: Settings,
        access_control: AccessControlService,
    ) -> None:
        self.db = db
        self.xui = xui
        self.settings = settings
        self.access_control = access_control
        self._locks: dict[int, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _user_lock(self, tg_id: int) -> asyncio.Lock:
        async with self._locks_guard:
            if tg_id not in self._locks:
                self._locks[tg_id] = asyncio.Lock()
            return self._locks[tg_id]

    async def ensure_user_access(self, tg_id: int) -> ProvisionResult:
        membership = await self.access_control.get_membership_status(tg_id)
        if not membership.get("allowed"):
            return ProvisionResult(
                allowed=False,
                status="denied",
                message="user is not a member of the access group",
                reason=str(membership.get("status") or membership.get("error") or "denied"),
            )

        user = self.db.get_user(tg_id)
        if user is not None:
            if user.status == "active" and user.inbound_id is not None:
                return ProvisionResult(
                    allowed=True,
                    status="active",
                    created=False,
                    inbound_id=user.inbound_id,
                    message="existing access",
                )
            if user.status in {"disabled", "expired", "unbound", "orphaned"}:
                return ProvisionResult(
                    allowed=True,
                    status=user.status,
                    inbound_id=user.inbound_id,
                    message=f"user status is {user.status}",
                    reason=user.status,
                )

        if not self.settings.auto_provision_inbound:
            return ProvisionResult(
                allowed=True,
                status="not_provisioned",
                message="automatic inbound provisioning is disabled",
                reason="auto_provision_disabled",
            )

        lock = await self._user_lock(tg_id)
        async with lock:
            if not self.db.acquire_provisioning_lock(tg_id):
                user = self.db.get_user(tg_id)
                return ProvisionResult(
                    allowed=True,
                    status=user.status if user is not None else "pending",
                    inbound_id=None if user is None else user.inbound_id,
                    message="provisioning already in progress",
                    reason="locked",
                )
            reserved_port: int | None = None
            try:
                user = self.db.get_user(tg_id)
                if user is not None:
                    if user.status == "active" and user.inbound_id is not None:
                        return ProvisionResult(
                            allowed=True,
                            status="active",
                            inbound_id=user.inbound_id,
                            message="existing access",
                        )
                    if user.status in {"disabled", "expired", "unbound", "orphaned"}:
                        return ProvisionResult(
                            allowed=True,
                            status=user.status,
                            inbound_id=user.inbound_id,
                            message=f"user status is {user.status}",
                            reason=user.status,
                        )
                else:
                    self.db.create_pending_user(tg_id, self.settings.max_configs_per_inbound)

                template = await self.find_template_inbound()
                template_id = int(template["id"])
                bound_template_user = self.db.get_user_by_inbound(template_id)
                if bound_template_user is not None:
                    raise RuntimeError(f"template inbound {template_id} is bound to Telegram ID {bound_template_user.tg_id}")

                template_before = await self.xui.get_inbound(template_id)
                if template_before is None:
                    raise RuntimeError(f"template inbound {template_id} disappeared")
                reserved_port = await self._reserve_free_port(tg_id)
                created_id = await self.xui.create_inbound_from_template_inbound(
                    template_inbound=template_before,
                    telegram_id=tg_id,
                    port=reserved_port,
                )
                if created_id == template_id:
                    raise RuntimeError("created inbound id matches template inbound id")

                verification = await self.xui.verify_created_inbound(
                    inbound_id=created_id,
                    template_inbound=template_before,
                    telegram_id=tg_id,
                    port=reserved_port,
                )
                if not verification.get("ok"):
                    raise RuntimeError("; ".join(verification.get("errors") or ["created inbound verification failed"]))

                template_after = await self.xui.get_inbound(template_id)
                if template_after is None:
                    raise RuntimeError("template inbound missing after provisioning")
                template_changes = compare_inbound_immutable_fields(template_before, template_after)
                if template_changes:
                    raise RuntimeError(f"template inbound changed: {','.join(template_changes)}")

                expires_at = int(time.time()) + self.settings.default_access_days * 86400
                self.db.upsert_user(
                    tg_id=tg_id,
                    inbound_id=created_id,
                    status="active",
                    expires_at=expires_at,
                    max_configs=self.settings.max_configs_per_inbound,
                    created_by="auto_group",
                    access_source="telegram_group",
                )
                self.db.set_port_allocation(reserved_port, "active", created_id)
                self.db.add_audit(
                    tg_id,
                    tg_id,
                    "auto_inbound_provisioned",
                    f"template_id={template_id};inbound_id={created_id};port={reserved_port}",
                )
                return ProvisionResult(
                    allowed=True,
                    status="active",
                    created=True,
                    inbound_id=created_id,
                    port=reserved_port,
                    message="access created",
                )
            except Exception as exc:
                if reserved_port is not None:
                    self.db.set_port_allocation(reserved_port, "failed", None)
                self.db.add_audit(tg_id, tg_id, "auto_inbound_provision_failed", str(exc))
                return ProvisionResult(
                    allowed=True,
                    status="failed",
                    message="failed to create access",
                    reason=str(exc),
                )
            finally:
                self.db.release_provisioning_lock(tg_id)

    async def find_template_inbound(self) -> dict[str, Any]:
        return await self.xui.find_template_inbound(
            template_inbound_id=self.settings.template_inbound_id,
            template_remark=self.settings.template_inbound_remark,
        )

    async def check_template(self) -> TemplateCheckResult:
        try:
            template = await self.find_template_inbound()
        except Exception as exc:
            return TemplateCheckResult(
                ok=False,
                found=False,
                reason=str(exc),
                inbound=None,
                link_supported=False,
                link_reason=str(exc),
            )
        try:
            build_share_link(
                inbound=template,
                client_uuid="00000000-0000-0000-0000-000000000000",
                email="tg_0_01",
                public_host=self.settings.public_host,
                name="dry-run",
            )
            link_supported = True
            link_reason = "supported"
        except LinkBuildError as exc:
            link_supported = False
            link_reason = str(exc)
        reason = "ok" if link_supported else link_reason
        return TemplateCheckResult(
            ok=link_supported,
            found=True,
            reason=reason,
            inbound=template,
            link_supported=link_supported,
            link_reason=link_reason,
        )

    async def auto_status(self) -> AutoProvisionStatus:
        used_ports = await self._used_ports()
        total_ports = self.settings.port_max - self.settings.port_min + 1
        used_in_range = {port for port in used_ports if self.settings.port_min <= port <= self.settings.port_max}
        return AutoProvisionStatus(
            enabled=self.settings.auto_provision_inbound,
            template_remark=self.settings.template_inbound_remark,
            port_min=self.settings.port_min,
            port_max=self.settings.port_max,
            used_ports=len(used_in_range),
            free_ports=max(0, total_ports - len(used_in_range)),
            auto_users=self.db.count_auto_users(),
        )

    async def planned_free_port(self) -> int:
        used_ports = await self._used_ports()
        for port in range(self.settings.port_min, self.settings.port_max + 1):
            if port not in used_ports:
                return port
        raise RuntimeError("no free port in configured range")

    async def _reserve_free_port(self, tg_id: int) -> int:
        for port in range(self.settings.port_min, self.settings.port_max + 1):
            used_ports = await self._used_ports()
            if port in used_ports:
                continue
            if self.db.reserve_port(port, tg_id):
                return port
        raise RuntimeError("no free port in configured range")

    async def _used_ports(self) -> set[int]:
        ports = set(self.db.allocated_ports(active_only=True))
        inbounds = await self.xui.list_inbounds()
        for inbound in inbounds:
            value = inbound.get("port")
            if value is None or not str(value).isdigit():
                continue
            ports.add(int(value))
        return ports
