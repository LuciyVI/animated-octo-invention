from __future__ import annotations

from bot.models import ConfigRecord


BTN_MENU = "Меню"
BTN_STATUS = "Статус доступа"
BTN_CONFIGS = "Мои устройства"
BTN_NEW_CONFIG = "Добавить устройство"
BTN_CREATE_ACCESS = "Проверить доступ"
BTN_ACCESS_SETTINGS = "Настройки доступа"
BTN_INBOUND_SETTINGS = "Настроить inbound"
BTN_REFRESH_STATUS = "Обновить статус"
BTN_BACK_TO_MENU = "Вернуться в меню"
BTN_DISABLE_MY_ACCESS = "Отключить мой доступ"
BTN_MY_ID = "Мой Telegram ID"
BTN_MY_ACCESS = "Мой доступ"
BTN_INSTRUCTION = "Инструкция"
BTN_API_CHECK = "API check"
BTN_LIST_INBOUNDS = "Список inbound"
BTN_CHECK_INBOUND = "Проверить inbound"
BTN_CREATE_INBOUND = "Создать inbound"
BTN_BIND = "Привязать"
BTN_SYNC = "Sync"
BTN_CHECK_TEMPLATE = "Проверить шаблон Moroz"
BTN_AUTO_STATUS = "Статус автосоздания"
BTN_USER_LIST = "Список пользователей"
BTN_FIND_USER = "Найти пользователя"

MENU_TEXT_TO_COMMAND = {
    BTN_MENU: "/menu",
    BTN_STATUS: "/status",
    BTN_CONFIGS: "/configs",
    BTN_NEW_CONFIG: "/new_config",
    BTN_CREATE_ACCESS: "/create_access",
    BTN_MY_ID: "/myid",
    BTN_MY_ACCESS: "/my_access",
    BTN_INSTRUCTION: "/instruction",
    BTN_ACCESS_SETTINGS: "/access_settings",
    BTN_INBOUND_SETTINGS: "/inbound_settings",
    BTN_REFRESH_STATUS: "/status",
    BTN_BACK_TO_MENU: "/menu",
    BTN_API_CHECK: "/api_check",
    BTN_LIST_INBOUNDS: "/list_inbounds",
    BTN_CHECK_INBOUND: "/check_inbound",
    BTN_CREATE_INBOUND: "/create_inbound",
    BTN_BIND: "/bind",
    BTN_SYNC: "/sync",
    BTN_CHECK_TEMPLATE: "/check_template",
    BTN_AUTO_STATUS: "/auto_status",
    BTN_USER_LIST: "/users",
    BTN_FIND_USER: "/user",
}


def denied_menu_keyboard():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CREATE_ACCESS)],
            [KeyboardButton(text=BTN_INSTRUCTION)],
            [KeyboardButton(text=BTN_MY_ID)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Проверьте доступ",
    )


def main_menu_keyboard(is_admin: bool = False, auto_provision: bool = False):
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    rows = [
        [KeyboardButton(text=BTN_CONFIGS), KeyboardButton(text=BTN_NEW_CONFIG)],
        [KeyboardButton(text=BTN_ACCESS_SETTINGS), KeyboardButton(text=BTN_INBOUND_SETTINGS)],
        [KeyboardButton(text=BTN_STATUS)],
        [KeyboardButton(text=BTN_MY_ACCESS), KeyboardButton(text=BTN_MY_ID)],
        [KeyboardButton(text=BTN_INSTRUCTION), KeyboardButton(text=BTN_MENU)],
    ]
    if not auto_provision:
        rows.insert(1, [KeyboardButton(text=BTN_CREATE_ACCESS)])
    if is_admin:
        rows.extend(
            [
                [KeyboardButton(text=BTN_API_CHECK), KeyboardButton(text=BTN_LIST_INBOUNDS)],
                [KeyboardButton(text=BTN_CHECK_INBOUND), KeyboardButton(text=BTN_CREATE_INBOUND)],
                [KeyboardButton(text=BTN_BIND), KeyboardButton(text=BTN_SYNC)],
                [KeyboardButton(text=BTN_CHECK_TEMPLATE), KeyboardButton(text=BTN_AUTO_STATUS)],
                [KeyboardButton(text=BTN_USER_LIST), KeyboardButton(text=BTN_FIND_USER)],
            ]
        )
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def menu_text_to_command(text: str) -> str | None:
    return MENU_TEXT_TO_COMMAND.get(text.strip())


def configs_keyboard(configs: list[ConfigRecord]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list[InlineKeyboardButton]] = []
    for number, config in enumerate(configs, start=1):
        if not config.enabled:
            continue
        rows.append(
            [
                InlineKeyboardButton(text=f"{number}. link", callback_data=f"cfg:link:{config.id}"),
                InlineKeyboardButton(text="QR", callback_data=f"cfg:qr:{config.id}"),
                InlineKeyboardButton(text="disable", callback_data=f"cfg:disable:{config.id}"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def new_config_confirm_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Создать конфигурацию", callback_data="menu:new_config")],
        ]
    )


def device_type_keyboard(action_id: str | None = None):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    def callback_data(title: str) -> str:
        if action_id:
            return f"device:title:{action_id}:{title}"
        return f"device:title:{title}"

    rows = [
        [
            InlineKeyboardButton(text="Телефон", callback_data=callback_data("phone")),
            InlineKeyboardButton(text="Ноутбук", callback_data=callback_data("laptop")),
        ],
        [
            InlineKeyboardButton(text="Роутер", callback_data=callback_data("router")),
            InlineKeyboardButton(text="Телевизор", callback_data=callback_data("tv")),
        ],
        [InlineKeyboardButton(text="Другое название", callback_data="device:custom")],
    ]
    if action_id:
        rows.append([InlineKeyboardButton(text="Отмена", callback_data=f"device:cancel:{action_id}")])
    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def device_confirm_keyboard(title: str, action_id: str | None = None):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    confirm_data = f"device:confirm:{action_id}" if action_id else f"device:confirm:{title}"
    choose_data = f"device:choose:{action_id}" if action_id else "device:choose"
    rows = [
        [InlineKeyboardButton(text="Подтвердить", callback_data=confirm_data)],
        [InlineKeyboardButton(text="Выбрать заново", callback_data=choose_data)],
    ]
    if action_id:
        rows.append([InlineKeyboardButton(text="Отмена", callback_data=f"device:cancel:{action_id}")])
    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def access_settings_keyboard(can_disable_inbound: bool = False):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [InlineKeyboardButton(text=BTN_CONFIGS, callback_data="menu:configs")],
        [InlineKeyboardButton(text=BTN_NEW_CONFIG, callback_data="device:choose")],
        [InlineKeyboardButton(text=BTN_INBOUND_SETTINGS, callback_data="inbound:show")],
        [InlineKeyboardButton(text=BTN_REFRESH_STATUS, callback_data="menu:status")],
        [InlineKeyboardButton(text=BTN_BACK_TO_MENU, callback_data="menu:show")],
    ]
    if can_disable_inbound:
        rows.append([InlineKeyboardButton(text=BTN_DISABLE_MY_ACCESS, callback_data="menu:disable_access")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inbound_settings_keyboard(can_edit: bool):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [InlineKeyboardButton(text="Обновить", callback_data="inbound:show")],
        [InlineKeyboardButton(text=BTN_BACK_TO_MENU, callback_data="menu:show")],
    ]
    if can_edit:
        rows.insert(0, [InlineKeyboardButton(text="Редактировать JSON", callback_data="inbound:edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inbound_edit_confirm_keyboard(action_id: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить изменение inbound", callback_data=f"inbound:confirm:{action_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"inbound:cancel:{action_id}")],
        ]
    )
