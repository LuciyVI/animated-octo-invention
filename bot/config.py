from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only without optional deps
    def load_dotenv(*_args: object, **_kwargs: object) -> bool:
        return False


def _parse_admin_ids(value: str) -> set[int]:
    ids: set[int] = set()
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        ids.add(int(part))
    return ids


def _parse_bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_optional_int(value: str) -> int | None:
    stripped = value.strip()
    if not stripped:
        return None
    return int(stripped)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: set[int]
    xui_host: str
    xui_token: str
    public_host: str
    max_configs_per_inbound: int = 5
    default_client_days: int = 30
    default_access_days: int = 30
    default_client_traffic_gb: int = 0
    db_path: str = "./bot.db"
    expiration_check_seconds: int = 600
    require_group_membership: bool = False
    access_group_id: int | None = None
    group_membership_cache_ttl_sec: int = 300
    disable_access_when_left_group: bool = False
    self_service_create_access: bool = False
    self_service_template_inbound_id: int | None = None
    auto_provision_inbound: bool = False
    template_inbound_remark: str = "Moroz"
    template_inbound_id: int | None = None
    port_min: int = 30000
    port_max: int = 39999
    max_inbounds_per_tg_id: int = 1
    user_can_manage_own_inbound: bool = True
    user_can_disable_own_clients: bool = True
    user_can_disable_own_inbound: bool = False
    user_can_change_inbound_core_settings: bool = False
    show_technical_ids_to_users: bool = False
    debug_callbacks: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        default_access_days = int(os.getenv("DEFAULT_ACCESS_DAYS", os.getenv("DEFAULT_CLIENT_DAYS", "30")))
        return cls(
            bot_token=os.getenv("BOT_TOKEN", "").strip(),
            admin_ids=_parse_admin_ids(os.getenv("ADMIN_IDS", "")),
            xui_host=os.getenv("XUI_HOST", "").strip(),
            xui_token=os.getenv("XUI_TOKEN", "").strip(),
            public_host=os.getenv("PUBLIC_HOST", "").strip(),
            max_configs_per_inbound=int(os.getenv("MAX_CONFIGS_PER_INBOUND", "5")),
            default_client_days=int(os.getenv("DEFAULT_CLIENT_DAYS", str(default_access_days))),
            default_access_days=default_access_days,
            default_client_traffic_gb=int(os.getenv("DEFAULT_CLIENT_TRAFFIC_GB", "0")),
            db_path=os.getenv("DB_PATH", "./bot.db").strip(),
            expiration_check_seconds=int(os.getenv("EXPIRATION_CHECK_SECONDS", "600")),
            require_group_membership=_parse_bool(os.getenv("REQUIRE_GROUP_MEMBERSHIP", "false")),
            access_group_id=_parse_optional_int(os.getenv("ACCESS_GROUP_ID", "")),
            group_membership_cache_ttl_sec=int(os.getenv("GROUP_MEMBERSHIP_CACHE_TTL_SEC", "300")),
            disable_access_when_left_group=_parse_bool(os.getenv("DISABLE_ACCESS_WHEN_LEFT_GROUP", "false")),
            self_service_create_access=_parse_bool(os.getenv("SELF_SERVICE_CREATE_ACCESS", "false")),
            self_service_template_inbound_id=_parse_optional_int(os.getenv("SELF_SERVICE_TEMPLATE_INBOUND_ID", "")),
            auto_provision_inbound=_parse_bool(os.getenv("AUTO_PROVISION_INBOUND", "false")),
            template_inbound_remark=os.getenv("TEMPLATE_INBOUND_REMARK", "Moroz").strip() or "Moroz",
            template_inbound_id=_parse_optional_int(os.getenv("TEMPLATE_INBOUND_ID", "")),
            port_min=int(os.getenv("PORT_MIN", "30000")),
            port_max=int(os.getenv("PORT_MAX", "39999")),
            max_inbounds_per_tg_id=int(os.getenv("MAX_INBOUNDS_PER_TG_ID", "1")),
            user_can_manage_own_inbound=_parse_bool(os.getenv("USER_CAN_MANAGE_OWN_INBOUND", "true"), True),
            user_can_disable_own_clients=_parse_bool(os.getenv("USER_CAN_DISABLE_OWN_CLIENTS", "true"), True),
            user_can_disable_own_inbound=_parse_bool(os.getenv("USER_CAN_DISABLE_OWN_INBOUND", "false")),
            user_can_change_inbound_core_settings=_parse_bool(
                os.getenv("USER_CAN_CHANGE_INBOUND_CORE_SETTINGS", "false")
            ),
            show_technical_ids_to_users=_parse_bool(os.getenv("SHOW_TECHNICAL_IDS_TO_USERS", "false")),
            debug_callbacks=_parse_bool(os.getenv("DEBUG_CALLBACKS", "false")),
        )

    def validate_runtime(self) -> None:
        missing = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.xui_host:
            missing.append("XUI_HOST")
        if not self.xui_token:
            missing.append("XUI_TOKEN")
        if not self.public_host:
            missing.append("PUBLIC_HOST")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        if self.max_configs_per_inbound <= 0:
            raise ValueError("MAX_CONFIGS_PER_INBOUND must be positive")
        if self.group_membership_cache_ttl_sec < 0:
            raise ValueError("GROUP_MEMBERSHIP_CACHE_TTL_SEC must be zero or positive")
        if self.default_access_days <= 0:
            raise ValueError("DEFAULT_ACCESS_DAYS must be positive")
        if self.port_min < 1 or self.port_max > 65535 or self.port_min > self.port_max:
            raise ValueError("PORT_MIN/PORT_MAX must define a valid port range")
        if self.max_inbounds_per_tg_id != 1:
            raise ValueError("MAX_INBOUNDS_PER_TG_ID must be 1")
        if self.require_group_membership and self.access_group_id is None:
            raise ValueError("ACCESS_GROUP_ID is required when REQUIRE_GROUP_MEMBERSHIP=true")
        if self.self_service_create_access and self.self_service_template_inbound_id is None:
            raise ValueError("SELF_SERVICE_TEMPLATE_INBOUND_ID is required when SELF_SERVICE_CREATE_ACCESS=true")
        if self.auto_provision_inbound and not self.template_inbound_remark and self.template_inbound_id is None:
            raise ValueError("TEMPLATE_INBOUND_REMARK or TEMPLATE_INBOUND_ID is required")
