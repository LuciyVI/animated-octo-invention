from __future__ import annotations

import asyncio
import json
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from bot.config import Settings
from bot.db import Database
from bot.link_builder import LinkBuildError, build_share_link
from bot.models import ConfigRecord, CreatedConfig, SyncReport
from bot.utils.security import is_admin_id
from bot.xui_client import (
    XUIError,
    build_inbound_update_payload,
    client_identity_set,
    compare_inbound_immutable_fields,
    compare_template_clone_fields,
    count_active_clients_for_tg,
    count_active_clients_in_inbound,
    extract_clients,
    extract_settings,
    parse_json_field,
)


class ServiceError(RuntimeError):
    pass


class PermissionDenied(ServiceError):
    pass


class ValidationError(ServiceError):
    pass


class UserNotFound(ServiceError):
    pass


class InboundNotFound(ServiceError):
    pass


class AlreadyBound(ServiceError):
    pass


class LimitExceeded(ServiceError):
    pass


class AccessDenied(ServiceError):
    pass


class ConfigNotFound(ServiceError):
    pass


def _safe_error_detail(exc: Exception) -> str:
    return str(exc).replace("\n", " ").strip()[:300]


@dataclass(frozen=True)
class UserSummary:
    tg_id: int
    status: str
    inbound_id: int | None
    expires_at: int | None
    max_configs: int
    local_configs: list[ConfigRecord]
    remote_active_clients: int | None


@dataclass(frozen=True)
class BindDryRunReport:
    target_tg_id: int
    inbound_id: int
    days: int
    expires_at: int
    remote_active_clients: int
    local_active_configs: int
    would_create_user: bool
    would_update_user: bool


@dataclass(frozen=True)
class NewConfigDryRunReport:
    target_tg_id: int
    inbound_id: int
    title: str
    email: str
    max_configs: int
    local_active_configs: int
    remote_active_clients: int
    telegram_prefix_active_clients: int
    link_supported: bool
    link_reason: str


@dataclass(frozen=True)
class CreateInboundDryRunReport:
    template_inbound_id: int
    target_tg_id: int
    days: int
    port: int
    remark: str
    protocol: str
    link_supported: bool
    link_reason: str
    existing_ports: list[int]


@dataclass(frozen=True)
class CreateInboundResult:
    inbound_id: int
    template_inbound_id: int
    target_tg_id: int
    port: int
    remark: str
    clone_warnings: list[str]


@dataclass(frozen=True)
class OwnInboundUpdateResult:
    inbound_id: int
    changed_fields: list[str]


class BotService:
    def __init__(self, db: Database, xui: object, settings: Settings) -> None:
        self.db = db
        self.xui = xui
        self.settings = settings
        self._locks: dict[int, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._create_inbound_guard = asyncio.Lock()

    async def _inbound_lock(self, inbound_id: int) -> asyncio.Lock:
        async with self._locks_guard:
            if inbound_id not in self._locks:
                self._locks[inbound_id] = asyncio.Lock()
            return self._locks[inbound_id]

    def _require_admin(self, actor_tg_id: int) -> None:
        if not is_admin_id(actor_tg_id, self.settings.admin_ids):
            raise PermissionDenied("admin command is not allowed for this Telegram ID")

    def _validate_positive_id(self, value: int, name: str) -> None:
        if value <= 0:
            raise ValidationError(f"{name} must be positive")

    def _validate_port(self, port: int) -> None:
        if port < 1 or port > 65535:
            raise ValidationError("port must be between 1 and 65535")

    def _template_identifier_matches(self, inbound: dict[str, Any]) -> bool:
        inbound_id = inbound.get("id")
        if self.settings.template_inbound_id is not None and inbound_id is not None:
            if int(inbound_id) == self.settings.template_inbound_id:
                return True
        return str(inbound.get("remark") or "") == self.settings.template_inbound_remark

    def _normalize_inbound_update_patch(
        self,
        current: dict[str, Any],
        patch: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        allowed_fields = {
            "enable",
            "remark",
            "listen",
            "port",
            "protocol",
            "expiryTime",
            "total",
            "settings",
            "streamSettings",
            "sniffing",
            "tag",
        }
        unknown_fields = sorted(str(key) for key in patch if str(key) not in allowed_fields and str(key) != "id")
        if unknown_fields:
            raise ValidationError(f"unsupported inbound fields: {', '.join(unknown_fields)}")

        base = build_inbound_update_payload(current)
        payload = deepcopy(base)
        for field in allowed_fields:
            if field in patch:
                payload[field] = deepcopy(patch[field])

        try:
            payload["port"] = int(payload["port"])
        except (TypeError, ValueError) as exc:
            raise ValidationError("port must be an integer") from exc
        self._validate_port(int(payload["port"]))

        for int_field in ("expiryTime", "total"):
            try:
                payload[int_field] = int(payload.get(int_field) or 0)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{int_field} must be an integer") from exc
        payload["enable"] = bool(payload.get("enable", True))
        payload["remark"] = str(payload.get("remark") or "")
        payload["listen"] = str(payload.get("listen") or "")
        payload["protocol"] = str(payload.get("protocol") or "")
        if not payload["protocol"]:
            raise ValidationError("protocol is required")

        settings = parse_json_field(payload.get("settings"), {})
        if not isinstance(settings, dict):
            raise ValidationError("settings must be a JSON object")
        current_clients = extract_settings(current).get("clients", [])
        settings["clients"] = current_clients if isinstance(current_clients, list) else []
        payload["settings"] = settings

        for object_field in ("streamSettings", "sniffing"):
            value = parse_json_field(payload.get(object_field), {})
            if not isinstance(value, dict):
                raise ValidationError(f"{object_field} must be a JSON object")
            payload[object_field] = value

        changed_fields = [
            field
            for field in allowed_fields
            if json.dumps(base.get(field), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            != json.dumps(payload.get(field), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        ]
        return payload, sorted(changed_fields)

    async def update_own_inbound_from_payload(
        self,
        tg_id: int,
        patch: dict[str, Any],
    ) -> OwnInboundUpdateResult:
        if not self.settings.user_can_change_inbound_core_settings:
            raise PermissionDenied("inbound core settings editing is disabled")
        user = self.db.get_user(tg_id)
        if user is None:
            raise UserNotFound("access has not been granted")
        if user.status != "active":
            raise AccessDenied(f"user status is {user.status}")
        if user.inbound_id is None:
            raise InboundNotFound("access is pending and inbound is not assigned")

        lock = await self._inbound_lock(user.inbound_id)
        async with lock:
            inbound = await self.xui.get_inbound(user.inbound_id)
            if inbound is None:
                self.db.set_user_status(tg_id, "orphaned")
                self.db.add_audit(None, tg_id, "orphaned", f"inbound_id={user.inbound_id}")
                raise InboundNotFound(f"inbound {user.inbound_id} not found")
            if self._template_identifier_matches(inbound):
                raise PermissionDenied("template inbound cannot be edited by user")

            payload, changed_fields = self._normalize_inbound_update_patch(inbound, patch)
            if not changed_fields:
                return OwnInboundUpdateResult(inbound_id=user.inbound_id, changed_fields=[])
            try:
                await self.xui.update_inbound(user.inbound_id, payload)
            except XUIError as exc:
                self.db.add_audit(
                    None,
                    tg_id,
                    "xui_update_inbound_failed",
                    f"inbound_id={user.inbound_id};fields={','.join(changed_fields)};"
                    f"error={_safe_error_detail(exc)}",
                )
                raise ServiceError(
                    "Не удалось обновить inbound из-за ошибки панели 3x-ui. "
                    "Администратор уже может посмотреть диагностику."
                ) from exc
            self.db.add_audit(
                tg_id,
                tg_id,
                "update_own_inbound",
                f"inbound_id={user.inbound_id};fields={','.join(changed_fields)}",
            )
            return OwnInboundUpdateResult(inbound_id=user.inbound_id, changed_fields=changed_fields)

    async def _resolve_new_inbound_port(self, template: dict, requested_port: str) -> tuple[int, list[int]]:
        inbounds = await self.xui.list_inbounds()
        used_ports = sorted(
            {
                int(inbound["port"])
                for inbound in inbounds
                if inbound.get("port") is not None and str(inbound.get("port")).isdigit()
            }
        )
        if requested_port.lower() != "auto":
            if not requested_port.isdigit():
                raise ValidationError("port must be a number or 'auto'")
            port = int(requested_port)
            self._validate_port(port)
            if port in used_ports:
                raise ValidationError(f"port {port} is already used by an existing inbound")
            return port, used_ports

        start = int(template.get("port") or 20000) + 1
        if start < 1024:
            start = 1024
        for port in range(start, 65536):
            if port not in used_ports:
                return port, used_ports
        for port in range(1024, start):
            if port not in used_ports:
                return port, used_ports
        raise ValidationError("no free TCP port found for inbound")

    async def _create_inbound_plan(
        self,
        actor_tg_id: int,
        template_inbound_id: int,
        target_tg_id: int,
        days: int,
        requested_port: str,
        remark: str,
        require_admin: bool = True,
    ) -> tuple[CreateInboundDryRunReport, dict]:
        if require_admin:
            self._require_admin(actor_tg_id)
        self._validate_positive_id(template_inbound_id, "template_inbound_id")
        self._validate_positive_id(target_tg_id, "telegram_id")
        if days <= 0:
            raise ValidationError("days must be positive")
        clean_remark = remark.strip()
        if not clean_remark:
            raise ValidationError("remark must not be empty")
        existing_user = self.db.get_user(target_tg_id)
        if existing_user is not None:
            raise AlreadyBound(f"Telegram ID {target_tg_id} is already bound to inbound {existing_user.inbound_id}")

        template = await self.xui.get_inbound(template_inbound_id)
        if template is None:
            raise InboundNotFound(f"template inbound {template_inbound_id} not found")

        port, used_ports = await self._resolve_new_inbound_port(template, requested_port)
        try:
            build_share_link(
                inbound=template,
                client_uuid="00000000-0000-0000-0000-000000000000",
                email=f"tg_{target_tg_id}_01",
                public_host=self.settings.public_host,
                name="dry-run",
            )
            link_supported = True
            link_reason = "supported"
        except LinkBuildError as exc:
            link_supported = False
            link_reason = str(exc)

        report = CreateInboundDryRunReport(
            template_inbound_id=template_inbound_id,
            target_tg_id=target_tg_id,
            days=days,
            port=port,
            remark=clean_remark,
            protocol=str(template.get("protocol") or ""),
            link_supported=link_supported,
            link_reason=link_reason,
            existing_ports=used_ports,
        )
        return report, template

    async def create_inbound_dry_run(
        self,
        actor_tg_id: int,
        template_inbound_id: int,
        target_tg_id: int,
        days: int,
        requested_port: str,
        remark: str,
    ) -> CreateInboundDryRunReport:
        report, _template = await self._create_inbound_plan(
            actor_tg_id,
            template_inbound_id,
            target_tg_id,
            days,
            requested_port,
            remark,
        )
        return report

    async def _create_inbound_from_template(
        self,
        actor_tg_id: int,
        template_inbound_id: int,
        target_tg_id: int,
        days: int,
        requested_port: str,
        remark: str,
        require_admin: bool,
        audit_action: str,
    ) -> CreateInboundResult:
        async with self._create_inbound_guard:
            report, template = await self._create_inbound_plan(
                actor_tg_id,
                template_inbound_id,
                target_tg_id,
                days,
                requested_port,
                remark,
                require_admin=require_admin,
            )

            created = await self.xui.create_inbound_from_template(
                template_inbound_id=template_inbound_id,
                port=report.port,
                remark=report.remark,
            )
            if created.get("id") is None:
                raise RuntimeError("created inbound has no id")
            created_id = int(created["id"])
            if created_id == template_inbound_id:
                raise RuntimeError("created inbound id matches template inbound id")
            if int(created.get("port") or 0) != report.port:
                raise RuntimeError("created inbound port does not match requested port")
            if str(created.get("remark") or "") != report.remark:
                raise RuntimeError("created inbound remark does not match requested remark")
            if extract_clients(created):
                raise RuntimeError("created inbound unexpectedly contains clients")

            clone_warnings = compare_template_clone_fields(template, created)
            if clone_warnings:
                self.db.add_audit(
                    None,
                    target_tg_id,
                    "critical_created_inbound_template_mismatch",
                    f"inbound_id={created_id};template_id={template_inbound_id};fields={','.join(clone_warnings)}",
                )

            expires_at = int(time.time()) + days * 86400
            self.db.upsert_user(
                tg_id=target_tg_id,
                inbound_id=created_id,
                status="active",
                expires_at=expires_at,
                max_configs=self.settings.max_configs_per_inbound,
            )
            self.db.add_audit(
                actor_tg_id,
                target_tg_id,
                audit_action,
                f"template_id={template_inbound_id};inbound_id={created_id};port={report.port};days={days}",
            )
            return CreateInboundResult(
                inbound_id=created_id,
                template_inbound_id=template_inbound_id,
                target_tg_id=target_tg_id,
                port=report.port,
                remark=report.remark,
                clone_warnings=clone_warnings,
            )

    async def create_inbound_from_template(
        self,
        actor_tg_id: int,
        template_inbound_id: int,
        target_tg_id: int,
        days: int,
        requested_port: str,
        remark: str,
    ) -> CreateInboundResult:
        return await self._create_inbound_from_template(
            actor_tg_id=actor_tg_id,
            template_inbound_id=template_inbound_id,
            target_tg_id=target_tg_id,
            days=days,
            requested_port=requested_port,
            remark=remark,
            require_admin=True,
            audit_action="create_inbound_from_template",
        )

    async def create_self_service_access(self, target_tg_id: int) -> CreateInboundResult:
        if not self.settings.self_service_create_access:
            raise ServiceError("self-service access creation is disabled")
        if self.settings.self_service_template_inbound_id is None:
            raise ServiceError("SELF_SERVICE_TEMPLATE_INBOUND_ID is not configured")
        return await self._create_inbound_from_template(
            actor_tg_id=target_tg_id,
            template_inbound_id=self.settings.self_service_template_inbound_id,
            target_tg_id=target_tg_id,
            days=self.settings.default_client_days,
            requested_port="auto",
            remark=f"tg_{target_tg_id}",
            require_admin=False,
            audit_action="create_self_service_access",
        )

    async def _validate_bind(
        self,
        actor_tg_id: int,
        target_tg_id: int,
        inbound_id: int,
        days: int,
    ) -> tuple[dict, int, object | None]:
        self._require_admin(actor_tg_id)
        self._validate_positive_id(target_tg_id, "telegram_id")
        self._validate_positive_id(inbound_id, "inbound_id")
        if days <= 0:
            raise ValidationError("days must be positive")

        inbound = await self.xui.get_inbound(inbound_id)
        if inbound is None:
            raise InboundNotFound(f"inbound {inbound_id} not found")
        if self.settings.template_inbound_id is not None and inbound_id == self.settings.template_inbound_id:
            raise ValidationError("template inbound cannot be bound to a user")
        if str(inbound.get("remark") or "") == self.settings.template_inbound_remark:
            raise ValidationError("template inbound cannot be bound to a user")

        existing_by_inbound = self.db.get_user_by_inbound(inbound_id)
        if existing_by_inbound is not None and existing_by_inbound.tg_id != target_tg_id:
            raise AlreadyBound(f"inbound {inbound_id} is already bound to {existing_by_inbound.tg_id}")

        existing_user = self.db.get_user(target_tg_id)
        if existing_user is not None and existing_user.inbound_id != inbound_id:
            raise AlreadyBound(f"Telegram ID {target_tg_id} is already bound to inbound {existing_user.inbound_id}")

        remote_active = count_active_clients_in_inbound(inbound)
        if remote_active > self.settings.max_configs_per_inbound:
            raise LimitExceeded(
                f"inbound {inbound_id} has {remote_active} active clients, limit is {self.settings.max_configs_per_inbound}"
            )
        return inbound, remote_active, existing_user

    async def bind_user(
        self,
        actor_tg_id: int,
        target_tg_id: int,
        inbound_id: int,
        days: int,
    ) -> None:
        await self._validate_bind(actor_tg_id, target_tg_id, inbound_id, days)
        expires_at = int(time.time()) + days * 86400
        self.db.upsert_user(
            tg_id=target_tg_id,
            inbound_id=inbound_id,
            status="active",
            expires_at=expires_at,
            max_configs=self.settings.max_configs_per_inbound,
        )
        self.db.add_audit(actor_tg_id, target_tg_id, "bind", f"inbound_id={inbound_id};days={days}")

    async def bind_dry_run(
        self,
        actor_tg_id: int,
        target_tg_id: int,
        inbound_id: int,
        days: int,
    ) -> BindDryRunReport:
        _inbound, remote_active, existing_user = await self._validate_bind(
            actor_tg_id,
            target_tg_id,
            inbound_id,
            days,
        )
        return BindDryRunReport(
            target_tg_id=target_tg_id,
            inbound_id=inbound_id,
            days=days,
            expires_at=int(time.time()) + days * 86400,
            remote_active_clients=remote_active,
            local_active_configs=self.db.count_enabled_configs_for_inbound(inbound_id),
            would_create_user=existing_user is None,
            would_update_user=existing_user is not None,
        )

    async def new_config_dry_run(
        self,
        actor_tg_id: int,
        target_tg_id: int,
        title: str | None = None,
    ) -> NewConfigDryRunReport:
        self._require_admin(actor_tg_id)
        user = self.db.get_user(target_tg_id)
        if user is None:
            raise UserNotFound("access has not been granted")
        if user.status != "active":
            raise AccessDenied(f"user status is {user.status}")
        if user.inbound_id is None:
            raise InboundNotFound("access is pending and inbound is not assigned")

        inbound = await self.xui.get_inbound(user.inbound_id)
        if inbound is None:
            raise InboundNotFound(f"inbound {user.inbound_id} not found")

        max_configs = min(user.max_configs, self.settings.max_configs_per_inbound)
        local_active = self.db.count_enabled_configs_for_inbound(user.inbound_id)
        remote_active = count_active_clients_in_inbound(inbound)
        prefix_active = count_active_clients_for_tg(inbound, target_tg_id)
        if local_active >= max_configs:
            raise LimitExceeded(f"local active config limit reached: {local_active}/{max_configs}")
        if remote_active >= max_configs:
            raise LimitExceeded(f"inbound active client limit reached: {remote_active}/{max_configs}")
        if prefix_active >= max_configs:
            raise LimitExceeded(f"Telegram ID client prefix limit reached: {prefix_active}/{max_configs}")

        index = self.db.next_config_index(target_tg_id)
        email = f"tg_{target_tg_id}_{index:02d}"
        clean_title = (title or "").strip() or f"config {index:02d}"
        try:
            build_share_link(
                inbound=inbound,
                client_uuid="00000000-0000-0000-0000-000000000000",
                email=email,
                public_host=self.settings.public_host,
                name=clean_title,
            )
            link_supported = True
            link_reason = "supported"
        except LinkBuildError as exc:
            link_supported = False
            link_reason = str(exc)

        return NewConfigDryRunReport(
            target_tg_id=target_tg_id,
            inbound_id=user.inbound_id,
            title=clean_title,
            email=email,
            max_configs=max_configs,
            local_active_configs=local_active,
            remote_active_clients=remote_active,
            telegram_prefix_active_clients=prefix_active,
            link_supported=link_supported,
            link_reason=link_reason,
        )

    async def create_config(self, tg_id: int, title: str | None = None) -> CreatedConfig:
        user = self.db.get_user(tg_id)
        if user is None:
            raise UserNotFound("access has not been granted")
        if user.status != "active":
            raise AccessDenied(f"user status is {user.status}")
        if user.inbound_id is None:
            raise InboundNotFound("access is pending and inbound is not assigned")

        lock = await self._inbound_lock(user.inbound_id)
        async with lock:
            inbound = await self.xui.get_inbound(user.inbound_id)
            if inbound is None:
                self.db.set_user_status(tg_id, "orphaned")
                self.db.add_audit(None, tg_id, "orphaned", f"inbound_id={user.inbound_id}")
                raise InboundNotFound(f"inbound {user.inbound_id} not found")

            max_configs = min(user.max_configs, self.settings.max_configs_per_inbound)
            local_active = self.db.count_enabled_configs_for_inbound(user.inbound_id)
            remote_active = count_active_clients_in_inbound(inbound)
            prefix_active = count_active_clients_for_tg(inbound, tg_id)
            if local_active >= max_configs:
                raise LimitExceeded(f"local active config limit reached: {local_active}/{max_configs}")
            if remote_active >= max_configs:
                raise LimitExceeded(f"inbound active client limit reached: {remote_active}/{max_configs}")
            if prefix_active >= max_configs:
                raise LimitExceeded(f"Telegram ID client prefix limit reached: {prefix_active}/{max_configs}")

            index = self.db.next_config_index(tg_id)
            client_uuid = str(uuid.uuid4())
            email = f"tg_{tg_id}_{index:02d}"
            clean_title = (title or "").strip() or f"config {index:02d}"

            try:
                share_link = build_share_link(
                    inbound=inbound,
                    client_uuid=client_uuid,
                    email=email,
                    public_host=self.settings.public_host,
                    name=clean_title,
                )
            except LinkBuildError as exc:
                raise ValidationError(str(exc)) from exc

            try:
                self.db.assert_configs_schema_valid()
            except RuntimeError as exc:
                self.db.add_audit(
                    None,
                    tg_id,
                    "db_schema_invalid",
                    f"inbound_id={user.inbound_id};error={_safe_error_detail(exc)}",
                )
                raise ServiceError("Локальная БД требует миграции. Client в 3x-ui не создавался.") from exc

            try:
                await self.xui.add_client(
                    inbound_id=user.inbound_id,
                    telegram_id=tg_id,
                    title=clean_title,
                    client_uuid=client_uuid,
                    email=email,
                    expires_at=user.expires_at,
                    traffic_gb=self.settings.default_client_traffic_gb,
                )
            except XUIError as exc:
                self.db.add_audit(
                    None,
                    tg_id,
                    "xui_add_client_failed",
                    f"inbound_id={user.inbound_id};email={email};error={_safe_error_detail(exc)}",
                )
                raise ServiceError(
                    "Не удалось создать конфигурацию из-за ошибки панели 3x-ui. "
                    "Администратор уже может посмотреть диагностику."
                ) from exc

            try:
                after_inbound = await self.xui.get_inbound(user.inbound_id)
                immutable_changes = (
                    ["inbound_missing_after_add"]
                    if after_inbound is None
                    else compare_inbound_immutable_fields(inbound, after_inbound)
                )
                client_integrity_warnings: list[str] = []
                if after_inbound is None:
                    client_integrity_warnings.append("clients_unverified_after_add")
                else:
                    before_clients = client_identity_set(inbound)
                    after_clients = client_identity_set(after_inbound)
                    if not before_clients.issubset(after_clients):
                        client_integrity_warnings.append("existing_clients_missing_after_add")
                    new_client_count = sum(
                        1
                        for client in extract_clients(after_inbound)
                        if str(client.get("id")) == client_uuid or str(client.get("email")) == email
                    )
                    if new_client_count != 1:
                        client_integrity_warnings.append("new_client_count_not_one")
                config = self.db.add_config(
                    tg_id=tg_id,
                    inbound_id=user.inbound_id,
                    client_uuid=client_uuid,
                    email=email,
                    title=clean_title,
                    share_link=share_link,
                )
            except Exception as exc:
                self.db.add_audit(
                    None,
                    tg_id,
                    "critical_config_local_save_failed",
                    f"inbound_id={user.inbound_id};email={email};uuid_tail={client_uuid[-6:]};"
                    f"error={_safe_error_detail(exc)}",
                )
                try:
                    await self.xui.disable_client(user.inbound_id, client_uuid)
                    self.db.add_audit(
                        None,
                        tg_id,
                        "orphan_remote_client_disabled",
                        f"inbound_id={user.inbound_id};email={email};uuid_tail={client_uuid[-6:]}",
                    )
                except Exception as disable_exc:
                    self.db.add_audit(
                        None,
                        tg_id,
                        "critical_orphan_remote_client_disable_failed",
                        f"inbound_id={user.inbound_id};email={email};uuid_tail={client_uuid[-6:]};"
                        f"error={_safe_error_detail(disable_exc)}",
                    )
                raise ServiceError(
                    "Конфигурация не сохранена из-за локальной ошибки. "
                    "Администратор уже может посмотреть диагностику."
                ) from exc
            self.db.add_audit(tg_id, tg_id, "create_config", f"inbound_id={user.inbound_id};email={email}")
            if immutable_changes:
                self.db.add_audit(
                    None,
                    tg_id,
                    "critical_inbound_immutable_changed",
                    f"inbound_id={user.inbound_id};fields={','.join(immutable_changes)}",
                )
            if client_integrity_warnings:
                self.db.add_audit(
                    None,
                    tg_id,
                    "critical_inbound_clients_changed",
                    f"inbound_id={user.inbound_id};warnings={','.join(client_integrity_warnings)}",
                )
            return CreatedConfig(
                config=config,
                share_link=share_link,
                immutable_changes=immutable_changes,
                client_integrity_warnings=client_integrity_warnings,
            )

    async def revoke_config_by_number(
        self,
        actor_tg_id: int | None,
        target_tg_id: int,
        number: int,
        require_admin: bool = False,
    ) -> ConfigRecord:
        if require_admin:
            if actor_tg_id is None:
                raise PermissionDenied("admin command requires actor Telegram ID")
            self._require_admin(actor_tg_id)
        elif actor_tg_id is not None and actor_tg_id != target_tg_id:
            raise PermissionDenied("users can revoke only their own configs")

        config = self.db.get_config_by_number(target_tg_id, number)
        if config is None:
            raise ConfigNotFound("config not found")
        await self.revoke_config(actor_tg_id, config, require_admin=False)
        return config

    async def revoke_config(
        self,
        actor_tg_id: int | None,
        config: ConfigRecord,
        require_admin: bool = False,
    ) -> None:
        if require_admin:
            if actor_tg_id is None:
                raise PermissionDenied("admin command requires actor Telegram ID")
            self._require_admin(actor_tg_id)
        if config.enabled:
            await self.xui.disable_client(config.inbound_id, config.client_uuid)
            self.db.set_config_enabled(config.id, False)
        self.db.add_audit(
            actor_tg_id,
            config.tg_id,
            "revoke_config",
            f"inbound_id={config.inbound_id};email={config.email}",
        )

    async def unbind_user(self, actor_tg_id: int, target_tg_id: int) -> None:
        self._require_admin(actor_tg_id)
        user = self.db.get_user(target_tg_id)
        if user is None:
            raise UserNotFound("user not found")
        if user.inbound_id is None:
            raise InboundNotFound("access is pending and inbound is not assigned")
        self.db.set_user_status(target_tg_id, "unbound")
        self.db.add_audit(actor_tg_id, target_tg_id, "unbind", f"inbound_id={user.inbound_id}")

    async def disable_user(self, actor_tg_id: int, target_tg_id: int) -> None:
        self._require_admin(actor_tg_id)
        user = self.db.get_user(target_tg_id)
        if user is None:
            raise UserNotFound("user not found")
        if user.inbound_id is None:
            self.db.set_user_status(target_tg_id, "disabled")
            self.db.add_audit(actor_tg_id, target_tg_id, "disable", "inbound_id=None")
            return
        await self.xui.disable_user_clients(user.inbound_id, target_tg_id)
        self.db.disable_user_configs(target_tg_id)
        self.db.set_user_status(target_tg_id, "disabled")
        self.db.add_audit(actor_tg_id, target_tg_id, "disable", f"inbound_id={user.inbound_id}")

    async def disable_user_due_to_group_leave(self, target_tg_id: int) -> None:
        user = self.db.get_user(target_tg_id)
        if user is None:
            raise UserNotFound("user not found")
        if user.inbound_id is None:
            self.db.set_user_status(target_tg_id, "disabled")
            self.db.add_audit(None, target_tg_id, "disabled_due_to_group_leave", "inbound_id=None")
            return
        await self.xui.disable_user_clients(user.inbound_id, target_tg_id)
        self.db.disable_user_configs(target_tg_id)
        self.db.set_user_status(target_tg_id, "disabled")
        self.db.add_audit(
            None,
            target_tg_id,
            "disabled_due_to_group_leave",
            f"inbound_id={user.inbound_id}",
        )

    async def extend_user(self, actor_tg_id: int, target_tg_id: int, days: int) -> int:
        self._require_admin(actor_tg_id)
        if days <= 0:
            raise ValidationError("days must be positive")
        user = self.db.get_user(target_tg_id)
        if user is None:
            raise UserNotFound("user not found")
        base = max(int(time.time()), user.expires_at or 0)
        expires_at = base + days * 86400
        new_status = "active" if user.status in {"active", "expired"} else user.status
        self.db.set_user_status_and_expiry(target_tg_id, new_status, expires_at)
        self.db.add_audit(actor_tg_id, target_tg_id, "extend", f"days={days}")
        return expires_at

    async def get_user_summary(self, actor_tg_id: int, target_tg_id: int) -> UserSummary:
        self._require_admin(actor_tg_id)
        user = self.db.get_user(target_tg_id)
        if user is None:
            raise UserNotFound("user not found")
        configs = self.db.list_configs(target_tg_id)
        inbound = None if user.inbound_id is None else await self.xui.get_inbound(user.inbound_id)
        remote_count = None if inbound is None or user.inbound_id is None else await self.xui.count_active_clients(user.inbound_id)
        return UserSummary(
            tg_id=user.tg_id,
            status=user.status,
            inbound_id=user.inbound_id,
            expires_at=user.expires_at,
            max_configs=user.max_configs,
            local_configs=configs,
            remote_active_clients=remote_count,
        )

    async def sync(self, actor_tg_id: int) -> SyncReport:
        self._require_admin(actor_tg_id)
        inbounds = await self.xui.list_inbounds()
        remote_ids = {int(inbound["id"]) for inbound in inbounds if "id" in inbound}
        orphaned: list[int] = []
        existing: list[int] = []
        for user in self.db.list_users():
            if user.status == "unbound":
                continue
            if user.inbound_id is None:
                continue
            if user.inbound_id in remote_ids:
                existing.append(user.tg_id)
                continue
            self.db.set_user_status(user.tg_id, "orphaned")
            self.db.add_audit(actor_tg_id, user.tg_id, "sync_orphaned", f"inbound_id={user.inbound_id}")
            orphaned.append(user.tg_id)
        return SyncReport(checked=len(existing) + len(orphaned), orphaned=orphaned, existing=existing)

    async def expire_users(self) -> int:
        expired = self.db.list_expired_active_users(int(time.time()))
        for user in expired:
            try:
                if user.inbound_id is not None:
                    await self.xui.disable_user_clients(user.inbound_id, user.tg_id)
            finally:
                self.db.disable_user_configs(user.tg_id)
                self.db.set_user_status(user.tg_id, "expired")
                self.db.add_audit(None, user.tg_id, "expire", f"inbound_id={user.inbound_id}")
        return len(expired)
