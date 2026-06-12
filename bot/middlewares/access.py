from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.access_control import AccessControlService
from bot.config import Settings
from bot.db import Database
from bot.keyboards import menu_text_to_command
from bot.services import BotService
from bot.utils.security import is_private_chat_type


GROUP_ALLOWED_COMMANDS = {"/group_id", "/access_check"}
PRIVATE_DIAGNOSTIC_COMMANDS = {
    "/start",
    "/menu",
    "/group_id",
    "/myid",
    "/my_access",
    "/create_access",
    "/instruction",
}


class GroupAccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            return await self._handle_message(handler, event, data)
        if isinstance(event, CallbackQuery):
            return await self._handle_callback(handler, event, data)
        return await handler(event, data)

    async def _handle_message(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        message: Message,
        data: dict[str, Any],
    ) -> Any:
        command = _message_command(message)
        if command is None:
            return await handler(message, data)

        if not is_private_chat_type(message.chat.type):
            if command in GROUP_ALLOWED_COMMANDS:
                return await handler(message, data)
            await message.answer("Для работы с конфигурациями напишите боту в личные сообщения.")
            return None

        if command in PRIVATE_DIAGNOSTIC_COMMANDS:
            return await handler(message, data)

        user_id = message.from_user.id if message.from_user else None
        if user_id is None:
            await message.answer("Не удалось определить Telegram ID.")
            return None

        access: AccessControlService | None = data.get("access_control")
        if access is None:
            return await handler(message, data)

        membership = await access.get_membership_status(user_id)
        if membership.get("allowed"):
            return await handler(message, data)

        await _disable_due_to_group_leave_if_needed(user_id, data)

        if command == "/start":
            await message.answer(
                f"Ваш Telegram ID: {user_id}\n"
                "Доступ запрещён. Для использования бота нужно состоять в разрешённой Telegram-группе."
            )
        else:
            await message.answer(
                "Доступ запрещён. Для использования бота нужно состоять в разрешённой Telegram-группе."
            )
        return None

    async def _handle_callback(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        callback: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        message = callback.message
        if message is not None and not is_private_chat_type(message.chat.type):
            await callback.answer("Для работы с конфигурациями напишите боту в личные сообщения.", show_alert=True)
            return None

        access: AccessControlService | None = data.get("access_control")
        if access is None or callback.from_user is None:
            return await handler(callback, data)

        membership = await access.get_membership_status(callback.from_user.id)
        if membership.get("allowed"):
            return await handler(callback, data)
        await _disable_due_to_group_leave_if_needed(callback.from_user.id, data)
        await callback.answer(
            "Доступ запрещён. Для использования бота нужно состоять в разрешённой Telegram-группе.",
            show_alert=True,
        )
        return None


def _message_command(message: Message) -> str | None:
    text = message.text or ""
    if not text.startswith("/"):
        return menu_text_to_command(text)
    command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
    return command


async def _disable_due_to_group_leave_if_needed(user_id: int, data: dict[str, Any]) -> None:
    settings: Settings | None = data.get("settings")
    if settings is None or not settings.disable_access_when_left_group:
        return
    db: Database | None = data.get("db")
    service: BotService | None = data.get("service")
    if db is None or service is None:
        return
    user = db.get_user(user_id)
    if user is None or user.status == "disabled":
        return
    try:
        await service.disable_user_due_to_group_leave(user_id)
    except Exception as exc:
        logging.warning("Failed to disable user_id=%s after group access denial: %s", user_id, exc)
