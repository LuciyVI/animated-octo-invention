from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from aiogram.exceptions import TelegramBadRequest

from bot.handlers.user import (
    DEVICE_ACTION_ID_KEY,
    DEVICE_PENDING_TITLE_KEY,
    config_callback,
    device_callback,
    menu_callback,
    safe_edit_message,
)
from bot.middlewares.access import GroupAccessMiddleware
from bot.models import ConfigRecord, CreatedConfig
from tests.fakes import make_settings


class FakeState:
    def __init__(self, data: dict[str, Any] | None = None, state: str | None = None) -> None:
        self.data = dict(data or {})
        self.state = state

    async def get_state(self) -> str | None:
        return self.state

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def clear(self) -> None:
        self.data.clear()
        self.state = None

    async def set_state(self, state: object) -> None:
        self.state = getattr(state, "state", str(state))

    async def update_data(self, **kwargs: Any) -> dict[str, Any]:
        self.data.update(kwargs)
        return dict(self.data)


class FakeMessage:
    def __init__(self, events: list[str] | None = None, edit_error: Exception | None = None) -> None:
        self.chat = SimpleNamespace(id=123456789, type="private")
        self.message_id = 10
        self.events = events if events is not None else []
        self.answers: list[dict[str, Any]] = []
        self.photos: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.edit_error = edit_error

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.events.append(f"message.answer:{text.splitlines()[0]}")
        self.answers.append({"text": text, **kwargs})

    async def answer_photo(self, photo: object, **kwargs: Any) -> None:
        self.events.append("message.photo")
        self.photos.append({"photo": photo, **kwargs})

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.events.append(f"message.edit:{text.splitlines()[0]}")
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append({"text": text, **kwargs})


class FakeCallback:
    def __init__(self, data: str, events: list[str] | None = None, message: FakeMessage | None = None) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=123456789)
        self.message = message if message is not None else FakeMessage(events)
        self.events = events if events is not None else self.message.events
        self.answers: list[dict[str, Any]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False, **kwargs: Any) -> None:
        self.events.append("callback.answer")
        self.answers.append({"text": text, "show_alert": show_alert, **kwargs})


class FakeService:
    def __init__(self, events: list[str] | None = None, error: Exception | None = None) -> None:
        self.events = events if events is not None else []
        self.error = error
        self.calls = 0

    async def create_config(self, tg_id: int, title: str | None = None) -> CreatedConfig:
        self.events.append("create_config")
        self.calls += 1
        if self.error is not None:
            raise self.error
        clean_title = title or "config 01"
        config = ConfigRecord(
            id=self.calls,
            tg_id=tg_id,
            inbound_id=7,
            client_uuid="00000000-0000-0000-0000-000000000000",
            email=f"tg_{tg_id}_{self.calls:02d}",
            title=clean_title,
            share_link="vless://redacted-for-test",
            enabled=True,
            created_at=1,
            updated_at=1,
        )
        return CreatedConfig(config=config, share_link=config.share_link or "")


def test_device_confirm_answers_callback_before_create_config(tmp_path):
    events: list[str] = []
    settings = make_settings(str(tmp_path / "bot.db"))
    state = FakeState({DEVICE_ACTION_ID_KEY: "a1", DEVICE_PENDING_TITLE_KEY: "laptop"})
    service = FakeService(events)
    callback = FakeCallback("device:confirm:a1", events)

    asyncio.run(device_callback(callback, service, settings, state))  # type: ignore[arg-type]

    assert service.calls == 1
    assert events.index("callback.answer") < events.index("create_config")


def test_config_callback_answers_before_sqlite_lookup(tmp_path):
    events: list[str] = []
    settings = make_settings(str(tmp_path / "bot.db"))
    callback = FakeCallback("cfg:link:1", events)

    class FakeDb:
        def get_config_by_id(self, config_id: int) -> ConfigRecord:
            events.append("db.get_config_by_id")
            return ConfigRecord(
                id=config_id,
                tg_id=123456789,
                inbound_id=7,
                client_uuid="00000000-0000-0000-0000-000000000000",
                email="tg_123456789_01",
                title="laptop",
                share_link="vless://redacted-for-test",
                enabled=True,
                created_at=1,
                updated_at=1,
            )

    asyncio.run(config_callback(callback, FakeDb(), object(), settings))  # type: ignore[arg-type]

    assert events.index("callback.answer") < events.index("db.get_config_by_id")


def test_menu_callback_answers_before_status_db_lookup(tmp_path):
    events: list[str] = []
    settings = make_settings(str(tmp_path / "bot.db"))
    callback = FakeCallback("menu:status", events)
    state = FakeState()

    class FakeDb:
        def get_user(self, tg_id: int) -> None:
            events.append("db.get_user")
            return None

    asyncio.run(menu_callback(callback, FakeDb(), settings, object(), state))  # type: ignore[arg-type]

    assert events.index("callback.answer") < events.index("db.get_user")


def test_device_title_stores_pending_title_without_creating_client(tmp_path):
    settings = make_settings(str(tmp_path / "bot.db"))
    state = FakeState({DEVICE_ACTION_ID_KEY: "a1"})
    service = FakeService()
    callback = FakeCallback("device:title:a1:laptop")

    asyncio.run(device_callback(callback, service, settings, state))  # type: ignore[arg-type]

    assert service.calls == 0
    assert state.data[DEVICE_PENDING_TITLE_KEY] == "laptop"
    assert callback.message.edits[-1]["text"] == "Создать устройство: laptop?"
    markup = callback.message.edits[-1]["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "device:confirm:a1"


def test_device_confirm_without_pending_title_does_not_create_client(tmp_path):
    settings = make_settings(str(tmp_path / "bot.db"))
    state = FakeState({DEVICE_ACTION_ID_KEY: "a1"})
    service = FakeService()
    callback = FakeCallback("device:confirm:a1")

    asyncio.run(device_callback(callback, service, settings, state))  # type: ignore[arg-type]

    assert service.calls == 0
    assert "Не удалось определить устройство" in callback.message.edits[-1]["text"]
    assert DEVICE_PENDING_TITLE_KEY not in state.data


def test_repeated_device_confirm_does_not_create_second_client(tmp_path):
    settings = make_settings(str(tmp_path / "bot.db"))
    state = FakeState({DEVICE_ACTION_ID_KEY: "a1", DEVICE_PENDING_TITLE_KEY: "laptop"})
    service = FakeService()
    callback = FakeCallback("device:confirm:a1")

    asyncio.run(device_callback(callback, service, settings, state))  # type: ignore[arg-type]
    asyncio.run(device_callback(callback, service, settings, state))  # type: ignore[arg-type]

    assert service.calls == 1
    assert callback.answers[0]["text"] is None
    assert callback.answers[1]["text"] is None


def test_stale_device_confirm_does_not_create_client(tmp_path):
    settings = make_settings(str(tmp_path / "bot.db"))
    state = FakeState({DEVICE_ACTION_ID_KEY: "live", DEVICE_PENDING_TITLE_KEY: "laptop"})
    service = FakeService()
    callback = FakeCallback("device:confirm:old")

    asyncio.run(device_callback(callback, service, settings, state))  # type: ignore[arg-type]

    assert service.calls == 0
    assert "Не удалось определить устройство" in callback.message.edits[-1]["text"]


def test_cancel_clears_device_fsm(tmp_path):
    settings = make_settings(str(tmp_path / "bot.db"))
    state = FakeState({DEVICE_ACTION_ID_KEY: "a1", DEVICE_PENDING_TITLE_KEY: "laptop"})
    service = FakeService()
    callback = FakeCallback("device:cancel:a1")

    asyncio.run(device_callback(callback, service, settings, state))  # type: ignore[arg-type]

    assert service.calls == 0
    assert state.data == {}
    assert state.state is None
    assert callback.message.edits[-1]["text"] == "Создание устройства отменено."


def test_safe_edit_message_ignores_message_not_modified():
    error = TelegramBadRequest(method=None, message="Bad Request: message is not modified")  # type: ignore[arg-type]
    callback = FakeCallback("device:choose", message=FakeMessage(edit_error=error))

    result = asyncio.run(safe_edit_message(callback, "Выберите тип устройства."))

    assert result is True


def test_group_access_denied_callback_answers_alert(tmp_path):
    called = False

    class FakeAccess:
        async def get_membership_status(self, user_id: int) -> dict[str, Any]:
            return {"allowed": False}

    async def handler(_event: object, _data: dict[str, Any]) -> None:
        nonlocal called
        called = True

    callback = FakeCallback("device:choose")
    middleware = GroupAccessMiddleware()

    asyncio.run(
        middleware._handle_callback(  # type: ignore[attr-defined]
            handler,
            callback,  # type: ignore[arg-type]
            {"access_control": FakeAccess(), "settings": make_settings(str(tmp_path / "bot.db"))},
        )
    )

    assert called is False
    assert callback.answers == [
        {
            "text": "Доступ запрещён. Для использования бота нужно состоять в разрешённой Telegram-группе.",
            "show_alert": True,
        }
    ]
