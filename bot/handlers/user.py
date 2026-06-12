from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from bot.access_control import AccessControlService
from bot.auto_provision import AutoProvisionService, ProvisionResult
from bot.config import Settings
from bot.db import Database
from bot.keyboards import (
    BTN_ACCESS_SETTINGS,
    BTN_CONFIGS,
    BTN_CREATE_ACCESS,
    BTN_MENU,
    BTN_MY_ACCESS,
    BTN_MY_ID,
    BTN_NEW_CONFIG,
    BTN_STATUS,
    BTN_INBOUND_SETTINGS,
    BTN_INSTRUCTION,
    access_settings_keyboard,
    configs_keyboard,
    denied_menu_keyboard,
    device_confirm_keyboard,
    device_type_keyboard,
    inbound_edit_confirm_keyboard,
    inbound_settings_keyboard,
    main_menu_keyboard,
)
from bot.services import (
    AccessDenied,
    AlreadyBound,
    ConfigNotFound,
    InboundNotFound,
    LimitExceeded,
    ServiceError,
    UserNotFound,
    ValidationError,
)
from bot.services import BotService
from bot.models import CreatedConfig
from bot.utils.qr import make_qr_png
from bot.utils.security import is_admin_id, is_private_chat_type
from bot.xui_client import count_active_clients_in_inbound, extract_clients

router = Router(name="user")
logger = logging.getLogger(__name__)

DEVICE_ACTION_ID_KEY = "device_action_id"
DEVICE_PENDING_TITLE_KEY = "pending_device_title"
DEVICE_IN_PROGRESS_KEY = "device_action_in_progress"
INBOUND_EDIT_ACTION_ID_KEY = "inbound_edit_action_id"
INBOUND_EDIT_PAYLOAD_KEY = "inbound_edit_payload"


class DeviceFlow(StatesGroup):
    choosing = State()
    confirming = State()


class InboundEditFlow(StatesGroup):
    waiting_json = State()
    confirming = State()


INSTRUCTION_TEXTS = (
    """Инструкция: общая схема

Бот работает с уже запущенной 3x-ui и использует Telegram-группу как whitelist.

Основная модель:
1. Пользователь открывает бота в личном чате.
2. Бот проверяет, что пользователь состоит в разрешённой Telegram-группе.
3. Если inbound ещё нет, бот автоматически создаёт отдельный inbound из template Moroz.
4. Созданный inbound привязывается к числовому Telegram ID.
5. Пользователь выпускает до 5 конфигураций устройств внутри своего inbound.

Что важно:
- Telegram username не используется как ключ.
- Один Telegram ID получает один inbound.
- В одном inbound можно создать максимум 5 active clients.
- Config links и QR отправляются только в личный чат.
- Template Moroz не привязывается к пользователю и не меняется ботом при выдаче доступа.""",
    """Как сконфигурировать inbound

Вариант 1. Настройка template Moroz администратором в 3x-ui:
1. Откройте панель 3x-ui.
2. Создайте или проверьте inbound с точным remark: Moroz.
3. Настройте protocol, port, streamSettings, TLS/REALITY, sniffing так, как должен работать будущий пользовательский inbound.
4. Убедитесь, что inbound Moroz реально работает на клиентском устройстве.
5. Убедитесь, что link builder поддерживает inbound: в Telegram админ может выполнить /test_link <id> или нажать Проверить шаблон Moroz.
6. В .env задайте:
AUTO_PROVISION_INBOUND=true
TEMPLATE_INBOUND_REMARK=Moroz
PORT_MIN=30000
PORT_MAX=39999
MAX_CONFIGS_PER_INBOUND=5
7. Если inbound-порты должны быть доступны снаружи, откройте нужный range в firewall/hosting/Docker.

При автосоздании бот копирует настройки из Moroz, но не копирует clients. Для пользователя меняются только remark, port, tag и пустой clients list.""",
    """Как настроить свой inbound через бота

Откройте плитку Настроить inbound.

По умолчанию экран read-only. Он показывает:
- remark;
- protocol;
- port;
- enable;
- количество clients.

Чтобы разрешить редактирование core-полей inbound, администратор должен включить в .env:
USER_CAN_CHANGE_INBOUND_CORE_SETTINGS=true

После перезапуска бота появится кнопка Редактировать JSON.

Порядок редактирования:
1. Нажмите Настроить inbound.
2. Нажмите Редактировать JSON.
3. Отправьте JSON object с полями, которые нужно изменить.
4. Проверьте список полей.
5. Нажмите Подтвердить изменение inbound.

Доступные поля:
enable, remark, listen, port, protocol, expiryTime, total, settings, streamSettings, sniffing, tag.

Примеры:
{"remark":"my-access"}
{"enable":true}
{"port":30000}

Ограничения безопасности:
- id игнорируется;
- settings.clients сохраняется текущим и не берётся из JSON;
- clients нужно выпускать через Добавить устройство, а не руками в JSON;
- изменение port/protocol/streamSettings/TLS/REALITY может сломать доступ;
- перед изменением core-полей сделайте backup базы 3x-ui.""",
    """Как выпустить конфигурацию устройства

1. Откройте бота в личном чате.
2. Нажмите /start или Меню.
3. Если доступ ещё не создан, бот проверит группу и автоматически создаст inbound.
4. Нажмите Добавить устройство.
5. Выберите тип устройства: Телефон, Ноутбук, Роутер, Телевизор или Другое название.
6. Проверьте название.
7. Нажмите Подтвердить.
8. Бот добавит client в ваш inbound, сохранит config в SQLite и отправит ссылку + QR.

Где посмотреть выпущенные configs:
- Нажмите Мои устройства.
- Для active config доступны link, QR и disable.

Лимиты:
- максимум 5 active configs на inbound;
- учитываются и записи SQLite, и реальные active clients в 3x-ui;
- вручную созданные clients в этом inbound тоже занимают лимит.

Если не создаётся config:
- проверьте Статус доступа;
- проверьте, что устройств меньше 5;
- проверьте, что вы пишете боту в private chat;
- админ может проверить /api_check, /check_inbound <id> и логи systemd.""",
)


def _from_user_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


def _format_ts(ts: int | None) -> str:
    if ts is None:
        return "не задан"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _command_arg_text(message: Message) -> str:
    text = message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _require_private(message: Message) -> bool:
    if not is_private_chat_type(message.chat.type):
        await message.answer("Команда доступна только в личном чате с ботом.")
        return False
    return True


def _service_error_text(exc: Exception) -> str:
    if isinstance(exc, UserNotFound):
        return "Доступ ещё не выдан. Передайте ваш Telegram ID администратору."
    if isinstance(exc, AlreadyBound):
        return f"Доступ уже существует: {exc}"
    if isinstance(exc, AccessDenied):
        return f"Создание конфигураций недоступно: {exc}"
    if isinstance(exc, LimitExceeded):
        return f"Лимит конфигураций исчерпан: {exc}"
    if isinstance(exc, InboundNotFound):
        return f"Привязанный inbound не найден: {exc}"
    if isinstance(exc, ConfigNotFound):
        return "Конфигурация не найдена."
    if isinstance(exc, ValidationError):
        return f"Операция невозможна: {exc}"
    if isinstance(exc, ServiceError):
        return str(exc)
    return "Операция не выполнена."


def _safe_callback_data(callback: CallbackQuery) -> str:
    return str(callback.data or "")


def _callback_user_id(callback: CallbackQuery) -> int | None:
    return callback.from_user.id if callback.from_user else None


async def _fsm_snapshot(state: FSMContext | None) -> tuple[str | None, list[str]]:
    if state is None:
        return None, []
    try:
        current_state = await state.get_state()
        data = await state.get_data()
    except Exception:
        logger.exception("failed to read FSM snapshot for callback diagnostics")
        return None, []
    return current_state, sorted(str(key) for key in data.keys())


async def _log_callback_stage(
    settings: Settings,
    callback: CallbackQuery,
    handler_name: str,
    stage: str,
    state: FSMContext | None = None,
    **extra: object,
) -> None:
    if not settings.debug_callbacks:
        return
    current_state, data_keys = await _fsm_snapshot(state)
    message = callback.message
    chat = message.chat if message is not None else None
    logger.info(
        "callback_flow stage=%s handler=%s user_id=%s chat_id=%s chat_type=%s "
        "message_id=%s data=%s state=%s fsm_keys=%s extra=%s",
        stage,
        handler_name,
        _callback_user_id(callback),
        chat.id if chat else None,
        chat.type if chat else None,
        message.message_id if message else None,
        _safe_callback_data(callback),
        current_state,
        data_keys,
        extra,
    )


async def _answer_callback(
    callback: CallbackQuery,
    settings: Settings,
    handler_name: str,
    state: FSMContext | None = None,
    text: str | None = None,
    show_alert: bool = False,
) -> bool:
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        logger.warning(
            "callback_answer_failed handler=%s user_id=%s data=%s error=%s",
            handler_name,
            _callback_user_id(callback),
            _safe_callback_data(callback),
            exc,
        )
        return False
    await _log_callback_stage(settings, callback, handler_name, "callback_answered", state)
    return True


def _message_is_not_modified(exc: TelegramBadRequest) -> bool:
    return "message is not modified" in str(exc).lower()


def _message_cannot_be_edited(exc: TelegramBadRequest) -> bool:
    message = str(exc).lower()
    return (
        "message can't be edited" in message
        or "message to edit not found" in message
        or "there is no text in the message to edit" in message
    )


async def safe_edit_message(callback: CallbackQuery, text: str, reply_markup: object | None = None) -> bool:
    if callback.message is None:
        return False
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if _message_is_not_modified(exc):
            return True
        if _message_cannot_be_edited(exc):
            logger.info(
                "callback_message_edit_skipped user_id=%s data=%s error=%s",
                _callback_user_id(callback),
                _safe_callback_data(callback),
                exc,
            )
            return False
        raise
    return True


async def _reply_or_edit(callback: CallbackQuery, text: str, reply_markup: object | None = None) -> None:
    if await safe_edit_message(callback, text, reply_markup=reply_markup):
        return
    if callback.message is not None:
        await callback.message.answer(text, reply_markup=reply_markup)


def _new_device_action_id() -> str:
    return secrets.token_hex(4)


async def _start_device_flow(state: FSMContext) -> str:
    action_id = _new_device_action_id()
    await state.clear()
    await state.set_state(DeviceFlow.choosing)
    await state.update_data(**{DEVICE_ACTION_ID_KEY: action_id})
    return action_id


async def _send_device_type_message(message: Message, state: FSMContext) -> None:
    action_id = await _start_device_flow(state)
    await message.answer(
        "Выберите тип устройства.",
        reply_markup=device_type_keyboard(action_id),
    )


async def _show_device_type_screen(
    callback: CallbackQuery,
    state: FSMContext,
    text: str = "Выберите тип устройства.",
) -> None:
    action_id = await _start_device_flow(state)
    await _reply_or_edit(callback, text, reply_markup=device_type_keyboard(action_id))


def _parse_device_title_callback(data: str) -> tuple[str | None, str | None]:
    parts = data.split(":", maxsplit=3)
    if len(parts) == 4 and parts[:2] == ["device", "title"]:
        return parts[2].strip() or None, parts[3].strip() or None
    parts = data.split(":", maxsplit=2)
    if len(parts) == 3 and parts[:2] == ["device", "title"]:
        return None, parts[2].strip() or None
    return None, None


def _parse_device_action_callback(data: str, action: str) -> str | None:
    prefix = f"device:{action}:"
    if data.startswith(prefix):
        return data[len(prefix):].strip() or None
    return None


def _payload_field_names(payload: dict[str, Any]) -> str:
    fields = [str(key) for key in payload.keys() if str(key) != "id"]
    return ", ".join(sorted(fields)) if fields else "нет полей"


def _inbound_summary(inbound: dict[str, Any], settings: Settings) -> str:
    clients = extract_clients(inbound)
    lines = [
        "Настройки inbound",
        "",
        f"remark: {inbound.get('remark', '')}",
        f"protocol: {inbound.get('protocol', '')}",
        f"port: {inbound.get('port', '')}",
        f"enable: {inbound.get('enable', inbound.get('enabled', 'unknown'))}",
        f"clients: {len(clients)} total, {count_active_clients_in_inbound(inbound)} active",
    ]
    if settings.show_technical_ids_to_users:
        lines.insert(2, f"inbound_id: {inbound.get('id')}")
    lines.extend(
        [
            "",
            "Редактирование core-настроек: "
            + ("включено" if settings.user_can_change_inbound_core_settings else "выключено"),
        ]
    )
    if not settings.user_can_change_inbound_core_settings:
        lines.extend(
            [
                "",
                "Администратор должен явно включить USER_CAN_CHANGE_INBOUND_CORE_SETTINGS=true.",
            ]
        )
    return "\n".join(lines)


def _inbound_edit_prompt() -> str:
    example = {
        "remark": "tg_123456789",
        "port": 30000,
        "enable": True,
        "protocol": "vless",
        "streamSettings": {"network": "tcp", "security": "reality"},
    }
    return (
        "Отправьте JSON с полями inbound, которые нужно изменить.\n\n"
        "Доступные поля:\n"
        "enable, remark, listen, port, protocol, expiryTime, total, settings, streamSettings, sniffing, tag.\n\n"
        "id игнорируется. settings.clients сохраняется текущим и через этот редактор не меняется.\n"
        "Секретные значения бот не печатает; если меняете REALITY/TLS, отправляйте полный корректный JSON вручную.\n\n"
        "Пример:\n"
        f"{json.dumps(example, ensure_ascii=False, indent=2)}"
    )


async def _send_own_inbound_settings(
    message: Message,
    tg_id: int,
    db: Database,
    settings: Settings,
    xui: object,
) -> None:
    user = db.get_user(tg_id)
    if user is None or user.inbound_id is None:
        await message.answer("Inbound ещё не привязан.")
        return
    inbound = await xui.get_inbound(user.inbound_id)
    if inbound is None:
        await message.answer("Привязанный inbound не найден в 3x-ui.")
        return
    await message.answer(
        _inbound_summary(inbound, settings),
        reply_markup=inbound_settings_keyboard(settings.user_can_change_inbound_core_settings),
    )


async def _show_own_inbound_settings(
    message: Message,
    db: Database,
    settings: Settings,
    xui: object,
) -> None:
    tg_id = _from_user_id(message)
    if tg_id is None:
        await message.answer("Не удалось определить Telegram ID.")
        return
    await _send_own_inbound_settings(message, tg_id, db, settings, xui)


async def _send_callback_failure(callback: CallbackQuery) -> None:
    if callback.message is not None:
        await callback.message.answer("Не удалось выполнить действие. Попробуйте ещё раз или вернитесь в меню.")


def _is_admin_message(message: Message, settings: Settings) -> bool:
    return is_admin_id(_from_user_id(message), settings.admin_ids)


async def _send_menu(message: Message, settings: Settings, text: str = "Выберите действие.") -> None:
    await message.answer(
        text,
        reply_markup=main_menu_keyboard(
            is_admin=_is_admin_message(message, settings),
            auto_provision=settings.auto_provision_inbound,
        ),
    )


async def _send_instruction(message: Message) -> None:
    for text in INSTRUCTION_TEXTS:
        await message.answer(text)


async def _send_created_config(message: Message, created: CreatedConfig, settings: Settings, tg_id: int) -> None:
    if settings.debug_callbacks:
        logger.info(
            "config_delivery stage=link_built user_id=%s config_id=%s inbound_id=%s title=%s",
            tg_id,
            created.config.id,
            created.config.inbound_id,
            created.config.title,
        )
    await message.answer(
        f"Конфигурация создана: {created.config.title}\n\n{created.share_link}"
    )
    qr_bytes = make_qr_png(created.share_link)
    if settings.debug_callbacks:
        logger.info(
            "config_delivery stage=qr_built user_id=%s config_id=%s qr_bytes=%s",
            tg_id,
            created.config.id,
            len(qr_bytes),
        )
    await message.answer_photo(
        BufferedInputFile(qr_bytes, filename="config.png"),
        caption="QR-код конфигурации",
    )
    if settings.debug_callbacks:
        logger.info(
            "config_delivery stage=message_sent user_id=%s config_id=%s",
            tg_id,
            created.config.id,
        )
    post_add_warnings = (created.immutable_changes or []) + (created.client_integrity_warnings or [])
    if post_add_warnings:
        warning = (
            "CRITICAL: inbound changed unexpectedly after /new_config.\n"
            f"Telegram ID: {tg_id}\n"
            f"inbound_id: {created.config.inbound_id}\n"
            f"warnings: {', '.join(post_add_warnings)}"
        )
        for admin_id in settings.admin_ids:
            try:
                await message.bot.send_message(admin_id, warning)
            except Exception:
                pass


def _user_status_lines(db: Database, settings: Settings, tg_id: int) -> list[str]:
    user = db.get_user(tg_id)
    if user is None:
        return ["Доступ ещё не создан."]
    active_configs = db.count_configs(tg_id, enabled_only=True)
    lines = [
        f"Статус: {user.status}",
        f"Устройств: {active_configs} из {user.max_configs}",
        f"Срок действия: {_format_ts(user.expires_at)}",
    ]
    if settings.show_technical_ids_to_users:
        lines.append(f"inbound_id: {user.inbound_id}")
    return lines


async def _ensure_access_and_show_menu(
    message: Message,
    db: Database,
    settings: Settings,
    auto_provision: AutoProvisionService,
) -> None:
    tg_id = _from_user_id(message)
    if tg_id is None:
        await message.answer("Не удалось определить Telegram ID.")
        return
    result = await auto_provision.ensure_user_access(tg_id)
    if not result.allowed:
        await message.answer(
            "Доступ закрыт\n\n"
            "Вы не состоите в разрешённой Telegram-группе.\n"
            "Для использования бота обратитесь к администратору.\n\n"
            f"Ваш Telegram ID: {tg_id}",
            reply_markup=denied_menu_keyboard(),
        )
        return
    if result.status in {"disabled", "expired", "unbound", "orphaned"}:
        await message.answer(
            "Доступ недоступен\n\n"
            f"Статус: {result.status}\n"
            "Обратитесь к администратору.",
            reply_markup=main_menu_keyboard(
                is_admin=_is_admin_message(message, settings),
                auto_provision=settings.auto_provision_inbound,
            ),
        )
        return
    if result.status == "failed":
        await message.answer(
            "Не удалось автоматически создать доступ.\n"
            "Обратитесь к администратору.",
            reply_markup=main_menu_keyboard(
                is_admin=_is_admin_message(message, settings),
                auto_provision=settings.auto_provision_inbound,
            ),
        )
        return
    if result.created:
        await message.answer(
            "Доступ создан\n\n"
            "Для вас автоматически создан отдельный доступ.\n"
            "Теперь можно добавить устройство.",
            reply_markup=main_menu_keyboard(
                is_admin=_is_admin_message(message, settings),
                auto_provision=settings.auto_provision_inbound,
            ),
        )
        return
    lines = ["Ваш доступ", "", *_user_status_lines(db, settings, tg_id), "", "Выберите действие."]
    await message.answer(
        "\n".join(lines),
        reply_markup=main_menu_keyboard(
            is_admin=_is_admin_message(message, settings),
            auto_provision=settings.auto_provision_inbound,
        ),
    )


@router.message(Command("start"))
async def cmd_start(
    message: Message,
    db: Database,
    settings: Settings,
    auto_provision: AutoProvisionService,
) -> None:
    if not await _require_private(message):
        return
    await _ensure_access_and_show_menu(message, db, settings, auto_provision)


@router.message(Command("menu"))
async def cmd_menu(message: Message, db: Database, settings: Settings, auto_provision: AutoProvisionService) -> None:
    if not await _require_private(message):
        return
    await _ensure_access_and_show_menu(message, db, settings, auto_provision)


@router.message(F.text == BTN_MENU)
async def tile_menu(message: Message, db: Database, settings: Settings, auto_provision: AutoProvisionService) -> None:
    if not await _require_private(message):
        return
    await _ensure_access_and_show_menu(message, db, settings, auto_provision)


@router.message(Command("instruction"))
async def cmd_instruction(message: Message) -> None:
    if not await _require_private(message):
        return
    await _send_instruction(message)


@router.message(F.text == BTN_INSTRUCTION)
async def tile_instruction(message: Message) -> None:
    await cmd_instruction(message)


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    tg_id = _from_user_id(message)
    if tg_id is None:
        await message.answer("Не удалось определить Telegram ID.")
        return
    await message.answer(str(tg_id))


@router.message(F.text == BTN_MY_ID)
async def tile_myid(message: Message) -> None:
    if not await _require_private(message):
        return
    await cmd_myid(message)


@router.message(Command("group_id"))
async def cmd_group_id(message: Message) -> None:
    if is_private_chat_type(message.chat.type):
        await message.answer("Добавьте бота в нужную группу и выполните /group_id там.")
        return
    await message.answer(
        f"Group ID: {message.chat.id}\n"
        f"Title: {message.chat.title or ''}"
    )


@router.message(Command("my_access"))
async def cmd_my_access(message: Message, access_control: AccessControlService) -> None:
    tg_id = _from_user_id(message)
    if tg_id is None:
        await message.answer("Не удалось определить Telegram ID.")
        return
    membership = await access_control.get_membership_status(tg_id)
    allowed = bool(membership.get("allowed"))
    status = membership.get("status") or "unknown"
    if membership.get("error") and not membership.get("admin_bypass"):
        status = "check_failed"
    await message.answer(
        "Ваш доступ:\n"
        f"Telegram ID: {tg_id}\n"
        f"Group membership: {status}\n"
        f"Allowed: {'yes' if allowed else 'no'}"
    )


@router.message(F.text == BTN_MY_ACCESS)
async def tile_my_access(message: Message, access_control: AccessControlService) -> None:
    if not await _require_private(message):
        return
    await cmd_my_access(message, access_control)


@router.message(Command("status"))
async def cmd_status(message: Message, db: Database, settings: Settings) -> None:
    tg_id = _from_user_id(message)
    if tg_id is None:
        await message.answer("Не удалось определить Telegram ID.")
        return
    user = db.get_user(tg_id)
    if user is None:
        await message.answer(
            f"Telegram ID: {tg_id}\n"
            "Доступ ещё не выдан. Передайте Telegram ID администратору."
        )
        return
    await message.answer("\n".join(_user_status_lines(db, settings, tg_id)))


@router.message(F.text == BTN_STATUS)
async def tile_status(message: Message, db: Database, settings: Settings) -> None:
    if not await _require_private(message):
        return
    await cmd_status(message, db, settings)


@router.message(Command("debug_state"))
async def cmd_debug_state(message: Message, db: Database, settings: Settings, state: FSMContext) -> None:
    if not await _require_private(message):
        return
    tg_id = _from_user_id(message)
    if tg_id is None:
        await message.answer("Не удалось определить Telegram ID.")
        return
    if not is_admin_id(tg_id, settings.admin_ids):
        await message.answer("Недостаточно прав.")
        return

    current_state = await state.get_state()
    data = await state.get_data()
    user = db.get_user(tg_id)
    configs_count = db.count_configs(tg_id, enabled_only=True) if user is not None else 0
    lines = [
        "Debug state",
        f"Telegram ID: {tg_id}",
        f"FSM state: {current_state}",
        f"FSM data keys: {', '.join(sorted(data.keys())) if data else 'none'}",
        f"pending_device_title: {data.get(DEVICE_PENDING_TITLE_KEY) or 'none'}",
        f"user status: {user.status if user else 'not_found'}",
        f"inbound_id: {user.inbound_id if user else 'none'}",
        f"configs count: {configs_count}",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("configs"))
async def cmd_configs(message: Message, db: Database) -> None:
    if not await _require_private(message):
        return
    tg_id = _from_user_id(message)
    if tg_id is None:
        await message.answer("Не удалось определить Telegram ID.")
        return
    user = db.get_user(tg_id)
    if user is None:
        await message.answer("Доступ ещё не выдан.")
        return
    configs = db.list_configs(tg_id)
    if not configs:
        await message.answer("Конфигураций пока нет.")
        return

    lines = ["Ваши конфигурации:", ""]
    for number, config in enumerate(configs, start=1):
        state = "active" if config.enabled else "disabled"
        lines.append(f"{number}. {config.title} — {state}")
    await message.answer("\n".join(lines), reply_markup=configs_keyboard(configs))


@router.message(F.text == BTN_CONFIGS)
async def tile_configs(message: Message, db: Database) -> None:
    await cmd_configs(message, db)


@router.message(Command("create_access"))
async def cmd_create_access(
    message: Message,
    service: BotService,
    settings: Settings,
    db: Database,
    auto_provision: AutoProvisionService,
) -> None:
    if not await _require_private(message):
        return
    tg_id = _from_user_id(message)
    if tg_id is None:
        await message.answer("Не удалось определить Telegram ID.")
        return
    if settings.auto_provision_inbound:
        await _ensure_access_and_show_menu(message, db, settings, auto_provision)
        return

    existing_user = db.get_user(tg_id)
    if existing_user is not None:
        await message.answer(
            "Доступ уже создан.\n"
            f"inbound_id: {existing_user.inbound_id}\n"
            f"Статус: {existing_user.status}\n"
            "Создать конфигурацию: /new_config phone\n"
            "Посмотреть конфигурации: /configs"
        )
        return

    if not settings.self_service_create_access:
        await message.answer(
            "Self-service создание доступа сейчас выключено.\n"
            "Администратор может создать доступ безопасной командой:\n"
            f"/create_inbound_dry_run <template_inbound_id> {tg_id} "
            f"{settings.default_client_days} auto tg_{tg_id}\n"
            f"/create_inbound <template_inbound_id> {tg_id} "
            f"{settings.default_client_days} auto tg_{tg_id}"
        )
        return
    if settings.self_service_template_inbound_id is None:
        await message.answer("Self-service создание доступа не настроено: не задан SELF_SERVICE_TEMPLATE_INBOUND_ID.")
        return

    try:
        result = await service.create_self_service_access(tg_id)
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return

    lines = [
        "Доступ создан.",
        f"inbound_id: {result.inbound_id}",
        f"template inbound: {result.template_inbound_id}",
        f"port: {result.port}",
        f"remark: {result.remark}",
        "Создать конфигурацию: /new_config phone",
    ]
    if result.clone_warnings:
        lines.extend(["", f"WARNING: cloned fields differ: {', '.join(result.clone_warnings)}"])
    await message.answer("\n".join(lines))


@router.message(F.text == BTN_CREATE_ACCESS)
async def tile_create_access(
    message: Message,
    service: BotService,
    settings: Settings,
    db: Database,
    auto_provision: AutoProvisionService,
) -> None:
    await cmd_create_access(message, service, settings, db, auto_provision)


@router.message(Command("new_config"))
async def cmd_new_config(message: Message, service: BotService, settings: Settings) -> None:
    if not await _require_private(message):
        return
    tg_id = _from_user_id(message)
    if tg_id is None:
        await message.answer("Не удалось определить Telegram ID.")
        return
    title = _command_arg_text(message) or None
    try:
        created = await service.create_config(tg_id, title=title)
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return

    await _send_created_config(message, created, settings, tg_id)


@router.message(F.text == BTN_NEW_CONFIG)
async def tile_new_config(message: Message, service: BotService, settings: Settings, state: FSMContext) -> None:
    if not await _require_private(message):
        return
    await _send_device_type_message(message, state)


@router.message(F.text == BTN_ACCESS_SETTINGS)
async def tile_access_settings(message: Message, db: Database, settings: Settings) -> None:
    if not await _require_private(message):
        return
    tg_id = _from_user_id(message)
    if tg_id is None:
        await message.answer("Не удалось определить Telegram ID.")
        return
    lines = [
        "Настройки доступа",
        "",
        *_user_status_lines(db, settings, tg_id),
        "",
        "Доступные действия:",
    ]
    await message.answer(
        "\n".join(lines),
        reply_markup=access_settings_keyboard(can_disable_inbound=settings.user_can_disable_own_inbound),
    )


@router.message(Command("inbound_settings"))
async def cmd_inbound_settings(message: Message, db: Database, settings: Settings, xui: object) -> None:
    if not await _require_private(message):
        return
    await _show_own_inbound_settings(message, db, settings, xui)


@router.message(F.text == BTN_INBOUND_SETTINGS)
async def tile_inbound_settings(message: Message, db: Database, settings: Settings, xui: object) -> None:
    await cmd_inbound_settings(message, db, settings, xui)


@router.message(InboundEditFlow.waiting_json)
async def inbound_edit_json_message(message: Message, settings: Settings, state: FSMContext) -> None:
    if not await _require_private(message):
        return
    raw = (message.text or "").strip()
    if raw.lower() in {"/cancel", "cancel", "отмена"}:
        await state.clear()
        await message.answer("Редактирование inbound отменено.")
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        await message.answer("Не удалось разобрать JSON. Отправьте корректный JSON object или /cancel.")
        return
    if not isinstance(payload, dict):
        await message.answer("JSON должен быть object, например {\"remark\": \"new-name\"}.")
        return
    action_id = _new_device_action_id()
    await state.set_state(InboundEditFlow.confirming)
    await state.update_data(
        **{
            INBOUND_EDIT_ACTION_ID_KEY: action_id,
            INBOUND_EDIT_PAYLOAD_KEY: payload,
        }
    )
    await message.answer(
        "Проверьте изменение inbound.\n\n"
        f"Поля к изменению: {_payload_field_names(payload)}\n\n"
        "settings.clients будет сохранён текущим и не будет взят из JSON.\n"
        "После подтверждения бот вызовет update inbound в 3x-ui.",
        reply_markup=inbound_edit_confirm_keyboard(action_id),
    )


@router.message(Command("delete_config"))
async def cmd_delete_config(message: Message, service: BotService) -> None:
    if not await _require_private(message):
        return
    tg_id = _from_user_id(message)
    if tg_id is None:
        await message.answer("Не удалось определить Telegram ID.")
        return
    arg = _command_arg_text(message)
    if not arg.isdigit():
        await message.answer("Использование: /delete_config <number>")
        return
    try:
        config = await service.revoke_config_by_number(tg_id, tg_id, int(arg))
    except Exception as exc:
        await message.answer(_service_error_text(exc))
        return
    await message.answer(f"Конфигурация отключена: {config.title}")


@router.callback_query(F.data.startswith("cfg:"))
async def config_callback(callback: CallbackQuery, db: Database, service: BotService, settings: Settings) -> None:
    handler_name = "config_callback"
    try:
        await _log_callback_stage(settings, callback, handler_name, "callback_received")
        if callback.message is None:
            await _answer_callback(callback, settings, handler_name)
            return
        if not is_private_chat_type(callback.message.chat.type):
            await callback.answer("Только в личном чате", show_alert=True)
            return
        if callback.from_user is None:
            await callback.answer("Не удалось определить Telegram ID", show_alert=True)
            return
        if not await _answer_callback(callback, settings, handler_name):
            return
        await _log_callback_stage(settings, callback, handler_name, "access_checked")

        parts = (callback.data or "").split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            await callback.message.answer("Некорректная команда.")
            return
        action = parts[1]
        config = db.get_config_by_id(int(parts[2]))
        if config is None or config.tg_id != callback.from_user.id:
            await callback.message.answer("Конфигурация не найдена.")
            return
        if not config.enabled and action in {"link", "qr", "disable"}:
            await callback.message.answer("Конфигурация уже отключена.")
            return

        await _log_callback_stage(settings, callback, handler_name, "action_started")
        if action == "link":
            if not config.share_link:
                await callback.message.answer("Ссылка недоступна.")
                return
            await callback.message.answer(config.share_link)
            await _log_callback_stage(settings, callback, handler_name, "message_sent")
            return

        if action == "qr":
            if not config.share_link:
                await callback.message.answer("QR недоступен.")
                return
            qr_bytes = make_qr_png(config.share_link)
            await _log_callback_stage(settings, callback, handler_name, "qr_built")
            await callback.message.answer_photo(
                BufferedInputFile(qr_bytes, filename="config.png"),
                caption=f"QR-код: {config.title}",
            )
            await _log_callback_stage(settings, callback, handler_name, "message_sent")
            return

        if action == "disable":
            try:
                await service.revoke_config(callback.from_user.id, config)
            except Exception as exc:
                await callback.message.answer(_service_error_text(exc))
                return
            await callback.message.answer(f"Конфигурация отключена: {config.title}")
            await _log_callback_stage(settings, callback, handler_name, "message_sent")
            return

        await callback.message.answer("Неизвестное действие.")
    except Exception:
        logger.exception(
            "callback handler failed: handler=%s data=%s user_id=%s",
            handler_name,
            _safe_callback_data(callback),
            _callback_user_id(callback),
        )
        await _log_callback_stage(settings, callback, handler_name, "handler_failed")
        await _send_callback_failure(callback)


@router.callback_query(F.data.startswith("device:"))
async def device_callback(
    callback: CallbackQuery,
    service: BotService,
    settings: Settings,
    state: FSMContext,
) -> None:
    handler_name = "device_callback"
    try:
        await _log_callback_stage(settings, callback, handler_name, "callback_received", state)
        if callback.message is None:
            await _answer_callback(callback, settings, handler_name, state)
            return
        if not is_private_chat_type(callback.message.chat.type):
            await callback.answer("Только в личном чате", show_alert=True)
            return
        if callback.from_user is None:
            await callback.answer("Не удалось определить Telegram ID", show_alert=True)
            return
        if not await _answer_callback(callback, settings, handler_name, state):
            return
        await _log_callback_stage(settings, callback, handler_name, "access_checked", state)

        data = callback.data or ""
        if data == "device:choose" or data.startswith("device:choose:"):
            await _show_device_type_screen(callback, state)
            await _log_callback_stage(settings, callback, handler_name, "handler_finished", state)
            return

        if data == "device:custom":
            await state.clear()
            await _reply_or_edit(
                callback,
                "Отправьте название командой:\n"
                "/new_config <название>\n\n"
                "Пример:\n"
                "/new_config ipad",
                reply_markup=None,
            )
            await _log_callback_stage(settings, callback, handler_name, "handler_finished", state)
            return

        cancel_action_id = _parse_device_action_callback(data, "cancel")
        if cancel_action_id is not None:
            await state.clear()
            await _reply_or_edit(callback, "Создание устройства отменено.", reply_markup=None)
            await _log_callback_stage(settings, callback, handler_name, "handler_finished", state)
            return

        if data.startswith("device:title:"):
            action_id, title = _parse_device_title_callback(data)
            fsm_data = await state.get_data()
            expected_action_id = fsm_data.get(DEVICE_ACTION_ID_KEY)
            await _log_callback_stage(settings, callback, handler_name, "fsm_loaded", state)
            if not title:
                await _show_device_type_screen(
                    callback,
                    state,
                    "Не удалось определить устройство. Выберите устройство заново.",
                )
                return
            if action_id is None:
                if expected_action_id:
                    await _show_device_type_screen(
                        callback,
                        state,
                        "Это устаревший выбор. Выберите устройство заново.",
                    )
                    return
                action_id = await _start_device_flow(state)
            elif action_id != expected_action_id:
                await _show_device_type_screen(
                    callback,
                    state,
                    "Это устаревший выбор. Выберите устройство заново.",
                )
                return

            await state.set_state(DeviceFlow.confirming)
            await state.update_data(
                **{
                    DEVICE_ACTION_ID_KEY: action_id,
                    DEVICE_PENDING_TITLE_KEY: title,
                    DEVICE_IN_PROGRESS_KEY: None,
                }
            )
            await _reply_or_edit(
                callback,
                f"Создать устройство: {title}?",
                reply_markup=device_confirm_keyboard(title, action_id),
            )
            await _log_callback_stage(settings, callback, handler_name, "handler_finished", state)
            return

        if data.startswith("device:confirm:"):
            action_id = _parse_device_action_callback(data, "confirm")
            fsm_data = await state.get_data()
            expected_action_id = fsm_data.get(DEVICE_ACTION_ID_KEY)
            title = fsm_data.get(DEVICE_PENDING_TITLE_KEY)
            in_progress = fsm_data.get(DEVICE_IN_PROGRESS_KEY)
            await _log_callback_stage(settings, callback, handler_name, "fsm_loaded", state)
            if action_id is None or action_id != expected_action_id or not title:
                await _show_device_type_screen(
                    callback,
                    state,
                    "Не удалось определить устройство. Выберите устройство заново.",
                )
                return
            if in_progress == action_id:
                await _reply_or_edit(callback, "Создание устройства уже выполняется.", reply_markup=None)
                return

            await state.update_data(**{DEVICE_IN_PROGRESS_KEY: action_id})
            await _log_callback_stage(settings, callback, handler_name, "action_started", state)
            await _reply_or_edit(callback, f"Создаю устройство: {title}...", reply_markup=None)
            try:
                await _log_callback_stage(settings, callback, handler_name, "xui_add_client_started", state)
                created = await service.create_config(callback.from_user.id, title=str(title))
                await _log_callback_stage(settings, callback, handler_name, "xui_add_client_finished", state)
                await _send_created_config(callback.message, created, settings, callback.from_user.id)
                await state.clear()
                await _reply_or_edit(callback, f"Устройство создано: {created.config.title}.", reply_markup=None)
                await _log_callback_stage(settings, callback, handler_name, "handler_finished", state)
                return
            except Exception as exc:
                logger.exception(
                    "callback handler failed: handler=%s data=%s user_id=%s",
                    handler_name,
                    _safe_callback_data(callback),
                    _callback_user_id(callback),
                )
                await _log_callback_stage(settings, callback, handler_name, "handler_failed", state)
                await state.clear()
                await _reply_or_edit(
                    callback,
                    "Не удалось выполнить действие. Попробуйте ещё раз или вернитесь в меню.\n\n"
                    f"{_service_error_text(exc)}",
                    reply_markup=None,
                )
                return

        await callback.message.answer("Неизвестное действие.")
    except Exception:
        logger.exception(
            "callback handler failed: handler=%s data=%s user_id=%s",
            handler_name,
            _safe_callback_data(callback),
            _callback_user_id(callback),
        )
        await _log_callback_stage(settings, callback, handler_name, "handler_failed", state)
        await state.clear()
        await _send_callback_failure(callback)


@router.callback_query(F.data.startswith("inbound:"))
async def inbound_callback(
    callback: CallbackQuery,
    db: Database,
    service: BotService,
    settings: Settings,
    xui: object,
    state: FSMContext,
) -> None:
    handler_name = "inbound_callback"
    try:
        await _log_callback_stage(settings, callback, handler_name, "callback_received", state)
        if callback.message is None:
            await _answer_callback(callback, settings, handler_name, state)
            return
        if not is_private_chat_type(callback.message.chat.type):
            await callback.answer("Только в личном чате", show_alert=True)
            return
        if callback.from_user is None:
            await callback.answer("Не удалось определить Telegram ID", show_alert=True)
            return
        if not await _answer_callback(callback, settings, handler_name, state):
            return

        data = callback.data or ""
        if data == "inbound:show":
            user = db.get_user(callback.from_user.id)
            if user is None or user.inbound_id is None:
                await _reply_or_edit(callback, "Inbound ещё не привязан.", reply_markup=None)
                return
            inbound = await xui.get_inbound(user.inbound_id)
            if inbound is None:
                await _reply_or_edit(callback, "Привязанный inbound не найден в 3x-ui.", reply_markup=None)
                return
            await _reply_or_edit(
                callback,
                _inbound_summary(inbound, settings),
                reply_markup=inbound_settings_keyboard(settings.user_can_change_inbound_core_settings),
            )
            return

        if data == "inbound:edit":
            if not settings.user_can_change_inbound_core_settings:
                await _reply_or_edit(
                    callback,
                    "Редактирование inbound выключено настройкой безопасности.\n"
                    "Нужно USER_CAN_CHANGE_INBOUND_CORE_SETTINGS=true.",
                    reply_markup=inbound_settings_keyboard(False),
                )
                return
            await state.clear()
            await state.set_state(InboundEditFlow.waiting_json)
            await _reply_or_edit(callback, _inbound_edit_prompt(), reply_markup=None)
            return

        if data.startswith("inbound:cancel:"):
            await state.clear()
            await _reply_or_edit(callback, "Редактирование inbound отменено.", reply_markup=None)
            return

        if data.startswith("inbound:confirm:"):
            action_id = data.rsplit(":", maxsplit=1)[-1]
            fsm_data = await state.get_data()
            expected_action_id = fsm_data.get(INBOUND_EDIT_ACTION_ID_KEY)
            payload = fsm_data.get(INBOUND_EDIT_PAYLOAD_KEY)
            if action_id != expected_action_id or not isinstance(payload, dict):
                await state.clear()
                await _reply_or_edit(
                    callback,
                    "Не удалось определить изменение inbound. Откройте настройку заново.",
                    reply_markup=None,
                )
                return
            try:
                result = await service.update_own_inbound_from_payload(callback.from_user.id, payload)
            except Exception as exc:
                await state.clear()
                await _reply_or_edit(
                    callback,
                    "Не удалось обновить inbound.\n\n"
                    f"{_service_error_text(exc)}",
                    reply_markup=None,
                )
                return
            await state.clear()
            changed = ", ".join(result.changed_fields) if result.changed_fields else "нет изменений"
            await _reply_or_edit(
                callback,
                "Inbound обновлён.\n"
                f"Изменённые поля: {changed}",
                reply_markup=inbound_settings_keyboard(settings.user_can_change_inbound_core_settings),
            )
            return

        await callback.message.answer("Неизвестное действие.")
    except Exception:
        logger.exception(
            "callback handler failed: handler=%s data=%s user_id=%s",
            handler_name,
            _safe_callback_data(callback),
            _callback_user_id(callback),
        )
        await _log_callback_stage(settings, callback, handler_name, "handler_failed", state)
        await state.clear()
        await _send_callback_failure(callback)


@router.callback_query(F.data.startswith("menu:"))
async def menu_callback(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    auto_provision: AutoProvisionService,
    state: FSMContext,
) -> None:
    handler_name = "menu_callback"
    try:
        await _log_callback_stage(settings, callback, handler_name, "callback_received", state)
        if callback.message is None:
            await _answer_callback(callback, settings, handler_name, state)
            return
        if not is_private_chat_type(callback.message.chat.type):
            await callback.answer("Только в личном чате", show_alert=True)
            return
        if callback.from_user is None:
            await callback.answer("Не удалось определить Telegram ID", show_alert=True)
            return
        if not await _answer_callback(callback, settings, handler_name, state):
            return

        parts = (callback.data or "").split(":", maxsplit=1)
        if len(parts) != 2:
            await callback.message.answer("Неизвестное действие.")
            return
        action = parts[1]
        await _log_callback_stage(settings, callback, handler_name, "action_started", state)
        if action == "show":
            await _ensure_access_and_show_menu(callback.message, db, settings, auto_provision)
        elif action == "status":
            await callback.message.answer("\n".join(_user_status_lines(db, settings, callback.from_user.id)))
        elif action == "configs":
            configs = db.list_configs(callback.from_user.id)
            if not configs:
                await callback.message.answer("Конфигураций пока нет.")
            else:
                lines = ["Ваши устройства:", ""]
                for number, config in enumerate(configs, start=1):
                    config_state = "active" if config.enabled else "disabled"
                    lines.append(f"{number}. {config.title} — {config_state}")
                await callback.message.answer("\n".join(lines), reply_markup=configs_keyboard(configs))
        elif action == "new_config":
            await _show_device_type_screen(callback, state)
        elif action == "disable_access":
            await callback.message.answer("Отключение собственного inbound сейчас запрещено настройкой безопасности.")
        else:
            await callback.message.answer("Неизвестное действие.")
            return
        await _log_callback_stage(settings, callback, handler_name, "handler_finished", state)
    except Exception:
        logger.exception(
            "callback handler failed: handler=%s data=%s user_id=%s",
            handler_name,
            _safe_callback_data(callback),
            _callback_user_id(callback),
        )
        await _log_callback_stage(settings, callback, handler_name, "handler_failed", state)
        await _send_callback_failure(callback)
