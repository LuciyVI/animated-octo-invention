from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.access_control import AccessControlService
from bot.auto_provision import AutoProvisionService
from bot.config import Settings
from bot.keyboards import (
    BTN_API_CHECK,
    BTN_AUTO_STATUS,
    BTN_BIND,
    BTN_CHECK_INBOUND,
    BTN_CHECK_TEMPLATE,
    BTN_CREATE_INBOUND,
    BTN_FIND_USER,
    BTN_LIST_INBOUNDS,
    BTN_SYNC,
    BTN_USER_LIST,
)
from bot.link_builder import explain_link_support
from bot.services import (
    AlreadyBound,
    ConfigNotFound,
    InboundNotFound,
    LimitExceeded,
    PermissionDenied,
    ServiceError,
    UserNotFound,
    ValidationError,
)
from bot.services import BotService
from bot.utils.security import is_admin_id
from bot.utils.security import mask_secret
from bot.xui_client import (
    count_active_clients_in_inbound,
    extract_clients,
    extract_stream_settings,
)

router = Router(name="admin")


def _actor_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


def _format_ts(ts: int | None) -> str:
    if ts is None:
        return "не задан"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _args(message: Message) -> list[str]:
    return (message.text or "").split()[1:]


async def _ensure_admin(message: Message, settings: Settings) -> bool:
    if not is_admin_id(_actor_id(message), settings.admin_ids):
        await message.answer("Недостаточно прав.")
        return False
    return True


def _service_error_text(exc: Exception) -> str:
    if isinstance(exc, PermissionDenied):
        return "Недостаточно прав."
    if isinstance(exc, AlreadyBound):
        return f"Привязка невозможна: {exc}"
    if isinstance(exc, LimitExceeded):
        return f"Лимит превышен: {exc}"
    if isinstance(exc, InboundNotFound):
        return f"Inbound не найден: {exc}"
    if isinstance(exc, UserNotFound):
        return "Пользователь не найден в БД."
    if isinstance(exc, ConfigNotFound):
        return "Конфигурация не найдена."
    if isinstance(exc, ValidationError):
        return f"Некорректная команда: {exc}"
    if isinstance(exc, ServiceError):
        return str(exc)
    return "Команда не выполнена."


def _first(values: Any) -> str:
    if isinstance(values, str):
        return values
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str) and value:
                return value
    return ""


def _inbound_details(inbound: dict[str, Any]) -> str:
    stream = extract_stream_settings(inbound)
    clients = extract_clients(inbound)
    tls = stream.get("tlsSettings") or {}
    reality = stream.get("realitySettings") or {}
    ws = stream.get("wsSettings") or {}
    if not isinstance(tls, dict):
        tls = {}
    if not isinstance(reality, dict):
        reality = {}
    if not isinstance(ws, dict):
        ws = {}
    reality_settings = reality.get("settings") or {}
    if not isinstance(reality_settings, dict):
        reality_settings = {}
    ws_headers = ws.get("headers") or {}
    if not isinstance(ws_headers, dict):
        ws_headers = {}

    sni = (
        str(tls.get("serverName") or "")
        or str(reality_settings.get("serverName") or "")
        or _first(reality.get("serverNames"))
        or str(ws_headers.get("Host") or "")
    )
    public_key = str(reality_settings.get("publicKey") or reality.get("publicKey") or "")
    supported, reason = explain_link_support(inbound)
    return "\n".join(
        [
            f"id: {inbound.get('id')}",
            f"remark: {inbound.get('remark', '')}",
            f"port: {inbound.get('port')}",
            f"protocol: {inbound.get('protocol')}",
            f"enable: {inbound.get('enable', inbound.get('enabled', 'unknown'))}",
            f"clients: {len(clients)} total, {count_active_clients_in_inbound(inbound)} active",
            f"stream type: {stream.get('network', 'tcp')}",
            f"security: {stream.get('security', '')}",
            f"serverName/SNI: {sni or 'не задан'}",
            f"reality public key: {mask_secret(public_key) if public_key else 'не задан'}",
            f"link generation supported: {'yes' if supported else 'no'}",
            f"link support reason: {reason}",
        ]
    )


@router.message(Command("list_inbounds"))
async def cmd_list_inbounds(message: Message, settings: Settings, xui: object) -> None:
    if not await _ensure_admin(message, settings):
        return
    inbounds = await xui.list_inbounds()
    if not inbounds:
        await message.answer("Inbound не найдены.")
        return
    lines = []
    for inbound in inbounds:
        clients = len(extract_clients(inbound))
        lines.append(
            "ID: {id} | remark: {remark} | port: {port} | protocol: {protocol} | clients: {clients}".format(
                id=inbound.get("id"),
                remark=inbound.get("remark", ""),
                port=inbound.get("port"),
                protocol=inbound.get("protocol"),
                clients=clients,
            )
        )
    await message.answer("\n".join(lines))


@router.message(F.text == BTN_LIST_INBOUNDS)
async def tile_list_inbounds(message: Message, settings: Settings, xui: object) -> None:
    await cmd_list_inbounds(message, settings, xui)


@router.message(Command("inbound"))
async def cmd_inbound(message: Message, settings: Settings, xui: object) -> None:
    if not await _ensure_admin(message, settings):
        return
    args = _args(message)
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: /inbound <inbound_id>")
        return
    inbound = await xui.get_inbound(int(args[0]))
    if inbound is None:
        await message.answer("Inbound не найден.")
        return
    await message.answer(_inbound_details(inbound))


@router.message(Command("test_link"))
async def cmd_test_link(message: Message, settings: Settings, xui: object) -> None:
    if not await _ensure_admin(message, settings):
        return
    args = _args(message)
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: /test_link <inbound_id>")
        return
    inbound = await xui.get_inbound(int(args[0]))
    if inbound is None:
        await message.answer("Inbound не найден.")
        return
    stream = extract_stream_settings(inbound)
    supported, reason = explain_link_support(inbound)
    await message.answer(
        "\n".join(
            [
                f"inbound_id: {inbound.get('id')}",
                f"protocol: {inbound.get('protocol')}",
                f"stream type: {stream.get('network', 'tcp')}",
                f"security: {stream.get('security', '')}",
                f"link generation supported: {'yes' if supported else 'no'}",
                f"reason: {reason}",
                "mode: readonly, no client created",
            ]
        )
    )


@router.message(Command("check_inbound"))
async def cmd_check_inbound(message: Message, settings: Settings, xui: object) -> None:
    if not await _ensure_admin(message, settings):
        return
    args = _args(message)
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: /check_inbound <inbound_id>")
        return
    inbound = await xui.get_inbound(int(args[0]))
    if inbound is None:
        await message.answer(f"inbound_id: {args[0]}\nexists: no")
        return
    clients = extract_clients(inbound)
    supported, reason = explain_link_support(inbound)
    await message.answer(
        "\n".join(
            [
                f"inbound_id: {inbound.get('id')}",
                "exists: yes",
                f"protocol: {inbound.get('protocol')}",
                f"port: {inbound.get('port')}",
                f"remark: {inbound.get('remark', '')}",
                f"clients count: {len(clients)}",
                f"active clients count: {count_active_clients_in_inbound(inbound)}",
                f"supported by link_builder: {'yes' if supported else 'no'}",
                f"reason: {reason}",
            ]
        )
    )


@router.message(F.text == BTN_CHECK_INBOUND)
async def tile_check_inbound(message: Message, settings: Settings) -> None:
    if not await _ensure_admin(message, settings):
        return
    await message.answer(
        "Введите команду с ID inbound:\n"
        "/check_inbound <inbound_id>\n\n"
        "Пример:\n"
        "/check_inbound 14"
    )


@router.message(Command("api_check"))
async def cmd_api_check(
    message: Message,
    settings: Settings,
    xui: object,
    auto_provision: AutoProvisionService | None = None,
) -> None:
    if not await _ensure_admin(message, settings):
        return
    report = await xui.api_check()
    endpoints = report.get("required_endpoints", {})
    lines = [
        "3x-ui API check",
        "",
        f"XUI_HOST: {settings.xui_host}",
        f"OpenAPI: {'OK' if report.get('openapi_ok') else 'FAIL'}",
        f"Bearer auth: {'OK' if report.get('auth_ok') else 'FAIL'}",
        f"Inbounds API: {'OK' if report.get('inbounds_ok') else 'FAIL'}",
        f"Inbounds count: {report.get('inbounds_count', 0)}",
        f"Client add API: {'OK' if report.get('client_add_ok') else 'FAIL'}"
        + (f" ({report.get('client_add_endpoint')})" if report.get("client_add_endpoint") else ""),
        f"Client update API: {'OK' if report.get('client_update_ok') else 'FAIL'}"
        + (f" ({report.get('client_update_endpoint')})" if report.get("client_update_endpoint") else ""),
        f"Inbound create API: {'OK' if report.get('inbound_create_ok') else 'FAIL'}"
        + (f" ({report.get('inbound_create_endpoint')})" if report.get("inbound_create_endpoint") else ""),
        "",
        "Required endpoints:",
    ]
    for key in [
        "GET /panel/api/inbounds/list",
        "GET /panel/api/inbounds/get/{id}",
        "POST /panel/api/clients/add",
        "POST /panel/api/clients/update/{email}",
        "POST /panel/api/inbounds/add",
        "POST /panel/api/inbounds/addClient",
        "POST /panel/api/inbounds/updateClient/{uuid}",
    ]:
        lines.append(f"- {key}: {'OK' if endpoints.get(key) else 'FAIL'}")
    errors = report.get("errors") or []
    if errors:
        lines.extend(["", "Errors:"])
        lines.extend(f"- {error}" for error in errors)
    if auto_provision is not None:
        template = await auto_provision.check_template()
        lines.extend(
            [
                "",
                "Template check:",
                f"- remark: {settings.template_inbound_remark}",
                f"- found: {'yes' if template.found else 'no'}",
                f"- can auto-provision: {'yes' if template.ok and settings.auto_provision_inbound else 'no'}",
                f"- reason: {template.reason}",
            ]
        )
    await message.answer("\n".join(lines))


@router.message(F.text == BTN_API_CHECK)
async def tile_api_check(
    message: Message,
    settings: Settings,
    xui: object,
    auto_provision: AutoProvisionService,
) -> None:
    await cmd_api_check(message, settings, xui, auto_provision)


@router.message(Command("check_template"))
async def cmd_check_template(message: Message, settings: Settings, auto_provision: AutoProvisionService) -> None:
    if not await _ensure_admin(message, settings):
        return
    result = await auto_provision.check_template()
    inbound = result.inbound or {}
    clients = extract_clients(inbound) if inbound else []
    lines = [
        f"Template: {settings.template_inbound_remark}",
        f"Found: {'yes' if result.found else 'no'}",
        f"Inbound ID: {inbound.get('id', 'не найден')}",
        f"Protocol: {inbound.get('protocol', 'unknown')}",
        f"Port: {inbound.get('port', 'unknown')}",
        f"Clients in template: {len(clients)}, will not be copied",
        f"Link builder support: {'yes' if result.link_supported else 'no'}",
        f"Can auto-provision: {'yes' if result.ok and settings.auto_provision_inbound else 'no'}",
        f"Reason: {result.reason}",
    ]
    await message.answer("\n".join(lines))


@router.message(F.text == BTN_CHECK_TEMPLATE)
async def tile_check_template(message: Message, settings: Settings, auto_provision: AutoProvisionService) -> None:
    await cmd_check_template(message, settings, auto_provision)


@router.message(Command("auto_status"))
async def cmd_auto_status(message: Message, settings: Settings, auto_provision: AutoProvisionService) -> None:
    if not await _ensure_admin(message, settings):
        return
    status = await auto_provision.auto_status()
    await message.answer(
        "\n".join(
            [
                f"AUTO_PROVISION_INBOUND: {'true' if status.enabled else 'false'}",
                f"TEMPLATE_INBOUND_REMARK: {status.template_remark}",
                f"PORT_MIN: {status.port_min}",
                f"PORT_MAX: {status.port_max}",
                f"Used ports: {status.used_ports}",
                f"Free ports: {status.free_ports}",
                f"Users with auto inbound: {status.auto_users}",
            ]
        )
    )


@router.message(F.text == BTN_AUTO_STATUS)
async def tile_auto_status(message: Message, settings: Settings, auto_provision: AutoProvisionService) -> None:
    await cmd_auto_status(message, settings, auto_provision)


@router.message(Command("access_check"))
async def cmd_access_check(message: Message, settings: Settings, access_control: AccessControlService) -> None:
    if not await _ensure_admin(message, settings):
        return
    args = _args(message)
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: /access_check <telegram_id>")
        return
    target_tg_id = int(args[0])
    membership = await access_control.get_membership_status(target_tg_id)
    lines = [
        "Access check",
        f"group id: {membership.get('group_id')}",
        f"user id: {target_tg_id}",
        f"raw status: {membership.get('status')}",
        f"is_member: {membership.get('is_member')}",
        f"allowed: {'yes' if membership.get('allowed') else 'no'}",
        f"cache hit: {'yes' if membership.get('cache_hit') else 'no'}",
    ]
    if membership.get("admin_bypass"):
        lines.append("admin bypass: yes")
    if membership.get("error"):
        lines.append(f"error: {membership.get('error')}")
    await message.answer("\n".join(lines))


@router.message(Command("bind"))
async def cmd_bind(message: Message, settings: Settings, service: BotService) -> None:
    if not await _ensure_admin(message, settings):
        return
    args = _args(message)
    if len(args) != 3 or not all(part.lstrip("-").isdigit() for part in args):
        await message.answer("Использование: /bind <telegram_id> <inbound_id> <days>")
        return
    try:
        await service.bind_user(
            actor_tg_id=_actor_id(message) or 0,
            target_tg_id=int(args[0]),
            inbound_id=int(args[1]),
            days=int(args[2]),
        )
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return
    await message.answer(f"Telegram ID {args[0]} привязан к inbound {args[1]}.")


@router.message(F.text == BTN_BIND)
async def tile_bind(message: Message, settings: Settings) -> None:
    if not await _ensure_admin(message, settings):
        return
    await message.answer(
        "Введите команду с Telegram ID, inbound_id и сроком:\n"
        "/bind <telegram_id> <inbound_id> <days>\n\n"
        "Безопасная проверка без изменений:\n"
        "/bind_dry_run <telegram_id> <inbound_id> <days>"
    )


@router.message(Command("bind_dry_run"))
async def cmd_bind_dry_run(message: Message, settings: Settings, service: BotService) -> None:
    if not await _ensure_admin(message, settings):
        return
    args = _args(message)
    if len(args) != 3 or not all(part.lstrip("-").isdigit() for part in args):
        await message.answer("Использование: /bind_dry_run <telegram_id> <inbound_id> <days>")
        return
    try:
        report = await service.bind_dry_run(
            actor_tg_id=_actor_id(message) or 0,
            target_tg_id=int(args[0]),
            inbound_id=int(args[1]),
            days=int(args[2]),
        )
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return
    await message.answer(
        "\n".join(
            [
                "DRY-RUN: no 3x-ui or SQLite changes were made.",
                f"telegram_id: {report.target_tg_id}",
                f"inbound_id: {report.inbound_id}",
                f"days: {report.days}",
                f"would expire at: {_format_ts(report.expires_at)}",
                f"remote active clients: {report.remote_active_clients}",
                f"local active configs: {report.local_active_configs}",
                f"would create user: {'yes' if report.would_create_user else 'no'}",
                f"would update user: {'yes' if report.would_update_user else 'no'}",
            ]
        )
    )


@router.message(Command("new_config_dry_run"))
async def cmd_new_config_dry_run(message: Message, settings: Settings, service: BotService) -> None:
    if not await _ensure_admin(message, settings):
        return
    text = message.text or ""
    parts = text.split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /new_config_dry_run <telegram_id> <title>")
        return
    title = parts[2] if len(parts) > 2 else None
    try:
        report = await service.new_config_dry_run(
            actor_tg_id=_actor_id(message) or 0,
            target_tg_id=int(parts[1]),
            title=title,
        )
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return
    await message.answer(
        "\n".join(
            [
                "DRY-RUN: no 3x-ui or SQLite changes were made.",
                f"telegram_id: {report.target_tg_id}",
                f"inbound_id: {report.inbound_id}",
                f"title: {report.title}",
                f"would create email: {report.email}",
                f"local active configs: {report.local_active_configs}/{report.max_configs}",
                f"remote active clients: {report.remote_active_clients}/{report.max_configs}",
                f"Telegram ID prefix clients: {report.telegram_prefix_active_clients}/{report.max_configs}",
                f"link supported: {'yes' if report.link_supported else 'no'}",
                f"link reason: {report.link_reason}",
            ]
        )
    )


def _parse_create_inbound_args(message: Message) -> tuple[int, int, int, str, str] | None:
    parts = (message.text or "").split(maxsplit=5)
    if len(parts) != 6:
        return None
    if not (parts[1].isdigit() and parts[2].isdigit() and parts[3].isdigit()):
        return None
    return int(parts[1]), int(parts[2]), int(parts[3]), parts[4], parts[5].strip()


@router.message(Command("create_inbound_dry_run"))
async def cmd_create_inbound_dry_run(message: Message, settings: Settings, service: BotService) -> None:
    if not await _ensure_admin(message, settings):
        return
    parsed = _parse_create_inbound_args(message)
    if parsed is None:
        await message.answer(
            "Использование: /create_inbound_dry_run <template_inbound_id> "
            "<telegram_id> <days> <port|auto> <remark>"
        )
        return
    template_id, target_tg_id, days, requested_port, remark = parsed
    try:
        report = await service.create_inbound_dry_run(
            actor_tg_id=_actor_id(message) or 0,
            template_inbound_id=template_id,
            target_tg_id=target_tg_id,
            days=days,
            requested_port=requested_port,
            remark=remark,
        )
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return
    await message.answer(
        "\n".join(
            [
                "DRY-RUN: no 3x-ui or SQLite changes were made.",
                f"template inbound: {report.template_inbound_id}",
                f"telegram_id: {report.target_tg_id}",
                f"days: {report.days}",
                f"new port: {report.port}",
                f"new remark: {report.remark}",
                f"protocol: {report.protocol}",
                f"link supported: {'yes' if report.link_supported else 'no'}",
                f"link reason: {report.link_reason}",
                "will clone template settings, streamSettings, sniffing and listen",
                "will clear template clients before creating inbound",
                "will bind Telegram ID to created inbound after creation",
            ]
        )
    )


@router.message(Command("create_inbound"))
async def cmd_create_inbound(message: Message, settings: Settings, service: BotService) -> None:
    if not await _ensure_admin(message, settings):
        return
    parsed = _parse_create_inbound_args(message)
    if parsed is None:
        await message.answer(
            "Использование: /create_inbound <template_inbound_id> "
            "<telegram_id> <days> <port|auto> <remark>"
        )
        return
    template_id, target_tg_id, days, requested_port, remark = parsed
    try:
        result = await service.create_inbound_from_template(
            actor_tg_id=_actor_id(message) or 0,
            template_inbound_id=template_id,
            target_tg_id=target_tg_id,
            days=days,
            requested_port=requested_port,
            remark=remark,
        )
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return
    lines = [
        "Inbound created and bound.",
        f"template inbound: {result.template_inbound_id}",
        f"new inbound_id: {result.inbound_id}",
        f"telegram_id: {result.target_tg_id}",
        f"port: {result.port}",
        f"remark: {result.remark}",
        "next: /new_config phone",
    ]
    if result.clone_warnings:
        lines.extend(["", f"WARNING: cloned fields differ: {', '.join(result.clone_warnings)}"])
    await message.answer("\n".join(lines))


@router.message(F.text == BTN_CREATE_INBOUND)
async def tile_create_inbound(message: Message, settings: Settings) -> None:
    if not await _ensure_admin(message, settings):
        return
    await message.answer(
        "Введите команду с template inbound, Telegram ID, сроком, port и remark:\n"
        "/create_inbound <template_inbound_id> <telegram_id> <days> <port|auto> <remark>\n\n"
        "Сначала проверьте dry-run без изменений:\n"
        "/create_inbound_dry_run <template_inbound_id> <telegram_id> <days> <port|auto> <remark>"
    )


@router.message(Command("unbind"))
async def cmd_unbind(message: Message, settings: Settings, service: BotService) -> None:
    if not await _ensure_admin(message, settings):
        return
    args = _args(message)
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: /unbind <telegram_id>")
        return
    try:
        await service.unbind_user(_actor_id(message) or 0, int(args[0]))
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return
    await message.answer(f"Telegram ID {args[0]} отвязан.")


@router.message(Command("disable"))
async def cmd_disable(message: Message, settings: Settings, service: BotService) -> None:
    if not await _ensure_admin(message, settings):
        return
    args = _args(message)
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: /disable <telegram_id>")
        return
    try:
        await service.disable_user(_actor_id(message) or 0, int(args[0]))
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return
    await message.answer(f"Telegram ID {args[0]} отключён.")


@router.message(Command("extend"))
async def cmd_extend(message: Message, settings: Settings, service: BotService) -> None:
    if not await _ensure_admin(message, settings):
        return
    args = _args(message)
    if len(args) != 2 or not all(part.lstrip("-").isdigit() for part in args):
        await message.answer("Использование: /extend <telegram_id> <days>")
        return
    try:
        expires_at = await service.extend_user(_actor_id(message) or 0, int(args[0]), int(args[1]))
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return
    await message.answer(f"Доступ продлён до {_format_ts(expires_at)}.")


@router.message(Command("user"))
async def cmd_user(message: Message, settings: Settings, service: BotService) -> None:
    if not await _ensure_admin(message, settings):
        return
    args = _args(message)
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: /user <telegram_id>")
        return
    try:
        summary = await service.get_user_summary(_actor_id(message) or 0, int(args[0]))
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return
    lines = [
        f"Telegram ID: {summary.tg_id}",
        f"status: {summary.status}",
        f"inbound_id: {summary.inbound_id}",
        f"expires_at: {_format_ts(summary.expires_at)}",
        f"max_configs: {summary.max_configs}",
        f"local configs: {len(summary.local_configs)}",
        f"active clients in inbound: {summary.remote_active_clients if summary.remote_active_clients is not None else 'inbound missing'}",
    ]
    if summary.local_configs:
        lines.append("configs:")
        for number, config in enumerate(summary.local_configs, start=1):
            state = "enabled" if config.enabled else "disabled"
            lines.append(f"{number}. {config.email} — {state}")
    await message.answer("\n".join(lines))


@router.message(Command("users"))
async def cmd_users(message: Message, settings: Settings, service: BotService) -> None:
    if not await _ensure_admin(message, settings):
        return
    users = service.db.list_users()
    if not users:
        await message.answer("Пользователи не найдены.")
        return
    lines = ["Пользователи:"]
    for user in users[:50]:
        lines.append(
            f"{user.tg_id} | status={user.status} | inbound={user.inbound_id} | source={user.access_source}"
        )
    if len(users) > 50:
        lines.append(f"... ещё {len(users) - 50}")
    await message.answer("\n".join(lines))


@router.message(F.text == BTN_USER_LIST)
async def tile_users(message: Message, settings: Settings, service: BotService) -> None:
    await cmd_users(message, settings, service)


@router.message(F.text == BTN_FIND_USER)
async def tile_find_user(message: Message, settings: Settings) -> None:
    if not await _ensure_admin(message, settings):
        return
    await message.answer(
        "Введите команду с Telegram ID:\n"
        "/user <telegram_id>\n\n"
        "Пример:\n"
        "/user 1452759621"
    )


@router.message(Command("revoke_config"))
async def cmd_revoke_config(message: Message, settings: Settings, service: BotService) -> None:
    if not await _ensure_admin(message, settings):
        return
    args = _args(message)
    if len(args) != 2 or not all(part.isdigit() for part in args):
        await message.answer("Использование: /revoke_config <telegram_id> <number>")
        return
    try:
        config = await service.revoke_config_by_number(
            actor_tg_id=_actor_id(message) or 0,
            target_tg_id=int(args[0]),
            number=int(args[1]),
            require_admin=True,
        )
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return
    await message.answer(f"Конфигурация отключена: {config.email}.")


@router.message(Command("sync"))
async def cmd_sync(message: Message, settings: Settings, service: BotService) -> None:
    if not await _ensure_admin(message, settings):
        return
    try:
        report = await service.sync(_actor_id(message) or 0)
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return
    await message.answer(
        "Sync completed.\n"
        f"Checked users: {report.checked}\n"
        f"Existing inbound users: {len(report.existing)}\n"
        f"Orphaned users: {len(report.orphaned)}"
        + (f"\nOrphaned Telegram IDs: {', '.join(map(str, report.orphaned))}" if report.orphaned else "")
    )


@router.message(F.text == BTN_SYNC)
async def tile_sync(message: Message, settings: Settings, service: BotService) -> None:
    await cmd_sync(message, settings, service)
