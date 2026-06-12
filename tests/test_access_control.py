from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import Chat, Message, User

from bot.access_control import AccessControlService
from bot.auto_provision import AutoProvisionService
from bot.config import Settings
from bot.handlers.admin import cmd_access_check
from bot.handlers.user import cmd_create_access, cmd_group_id
from bot.keyboards import BTN_API_CHECK, BTN_CONFIGS, BTN_INSTRUCTION, main_menu_keyboard, menu_text_to_command
from bot.middlewares.access import GroupAccessMiddleware
from tests.fakes import ADMIN_ID, FakeXUIService, make_reality_inbound, make_service, make_settings


class FakeTelegramBot:
    def __init__(self, result: object | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def get_chat_member(self, chat_id: int, user_id: int) -> object:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def settings_with_group(
    tmp_path,
    require: bool = True,
    ttl: int = 300,
    admin_ids: set[int] | None = None,
) -> Settings:
    base = make_settings(str(tmp_path / "bot.db"), admin_ids=admin_ids or {ADMIN_ID})
    return Settings(
        bot_token=base.bot_token,
        admin_ids=base.admin_ids,
        xui_host=base.xui_host,
        xui_token=base.xui_token,
        public_host=base.public_host,
        max_configs_per_inbound=base.max_configs_per_inbound,
        default_client_days=base.default_client_days,
        default_client_traffic_gb=base.default_client_traffic_gb,
        db_path=base.db_path,
        expiration_check_seconds=base.expiration_check_seconds,
        require_group_membership=require,
        access_group_id=-1001234567890 if require else None,
        group_membership_cache_ttl_sec=ttl,
        disable_access_when_left_group=False,
    )


@pytest.mark.parametrize(
    ("status", "is_member", "allowed"),
    [
        ("member", None, True),
        ("administrator", None, True),
        ("creator", None, True),
        ("left", None, False),
        ("kicked", None, False),
        ("restricted", True, True),
        ("restricted", False, False),
    ],
)
def test_membership_statuses(tmp_path, status: str, is_member: bool | None, allowed: bool):
    settings = settings_with_group(tmp_path)
    bot = FakeTelegramBot(SimpleNamespace(status=status, is_member=is_member))
    access = AccessControlService(bot, settings)  # type: ignore[arg-type]

    result = asyncio.run(access.get_membership_status(123))

    assert result["allowed"] is allowed
    assert result["status"] == status
    assert result["is_member"] is is_member


def test_require_group_membership_false_allows_user(tmp_path):
    settings = settings_with_group(tmp_path, require=False)
    bot = FakeTelegramBot(SimpleNamespace(status="left"))
    access = AccessControlService(bot, settings)  # type: ignore[arg-type]

    assert asyncio.run(access.is_allowed_user(123)) is True
    assert bot.calls == 0


def test_require_group_membership_without_access_group_fails_runtime_validation(tmp_path):
    base = make_settings(str(tmp_path / "bot.db"))
    settings = Settings(
        bot_token=base.bot_token,
        admin_ids=base.admin_ids,
        xui_host=base.xui_host,
        xui_token=base.xui_token,
        public_host=base.public_host,
        require_group_membership=True,
        access_group_id=None,
    )

    with pytest.raises(ValueError, match="ACCESS_GROUP_ID"):
        settings.validate_runtime()


def test_self_service_enabled_without_template_fails_runtime_validation(tmp_path):
    base = make_settings(str(tmp_path / "bot.db"))
    settings = Settings(
        bot_token=base.bot_token,
        admin_ids=base.admin_ids,
        xui_host=base.xui_host,
        xui_token=base.xui_token,
        public_host=base.public_host,
        self_service_create_access=True,
        self_service_template_inbound_id=None,
    )

    with pytest.raises(ValueError, match="SELF_SERVICE_TEMPLATE_INBOUND_ID"):
        settings.validate_runtime()


def test_admin_bypasses_group_membership(tmp_path):
    settings = settings_with_group(tmp_path)
    bot = FakeTelegramBot(SimpleNamespace(status="left"))
    access = AccessControlService(bot, settings)  # type: ignore[arg-type]

    result = asyncio.run(access.get_membership_status(ADMIN_ID))

    assert result["allowed"] is True
    assert result["admin_bypass"] is True
    assert bot.calls == 0


def test_membership_cache_hit_and_ttl_invalidation(tmp_path):
    settings = settings_with_group(tmp_path, ttl=300)
    bot = FakeTelegramBot(SimpleNamespace(status="member"))
    access = AccessControlService(bot, settings)  # type: ignore[arg-type]

    first = asyncio.run(access.get_membership_status(123))
    second = asyncio.run(access.get_membership_status(123))
    asyncio.run(access.invalidate_user_cache(123))
    third = asyncio.run(access.get_membership_status(123))

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert third["cache_hit"] is False
    assert bot.calls == 2


def test_membership_cache_ttl_expired_rechecks(tmp_path):
    settings = settings_with_group(tmp_path, ttl=0)
    bot = FakeTelegramBot(SimpleNamespace(status="member"))
    access = AccessControlService(bot, settings)  # type: ignore[arg-type]

    asyncio.run(access.get_membership_status(123))
    asyncio.run(access.get_membership_status(123))

    assert bot.calls == 2


def test_telegram_api_error_denies_user_and_reports_error(tmp_path):
    settings = settings_with_group(tmp_path)
    bot = FakeTelegramBot(error=RuntimeError("group unavailable"))
    access = AccessControlService(bot, settings)  # type: ignore[arg-type]

    result = asyncio.run(access.get_membership_status(123))

    assert result["allowed"] is False
    assert result["error"] == "group unavailable"


def make_message(text: str, chat_type: str = "private", user_id: int = 123, chat_id: int | None = None) -> Message:
    chat = Chat(id=chat_id if chat_id is not None else user_id, type=chat_type, title="Test Group")
    user = User(id=user_id, is_bot=False, first_name="Test")
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
    )


def test_group_id_in_group_returns_chat_id(monkeypatch: pytest.MonkeyPatch):
    answers: list[str] = []

    async def fake_answer(self: Message, text: str, **_kwargs: Any) -> None:
        answers.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)

    asyncio.run(cmd_group_id(make_message("/group_id", chat_type="supergroup", chat_id=-10042)))

    assert "Group ID: -10042" in answers[0]
    assert "Title: Test Group" in answers[0]


def test_group_id_in_private_explains_group_usage(monkeypatch: pytest.MonkeyPatch):
    answers: list[str] = []

    async def fake_answer(self: Message, text: str, **_kwargs: Any) -> None:
        answers.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)

    asyncio.run(cmd_group_id(make_message("/group_id")))

    assert "выполните /group_id там" in answers[0]


def test_access_check_available_to_admin(monkeypatch: pytest.MonkeyPatch, tmp_path):
    answers: list[str] = []

    async def fake_answer(self: Message, text: str, **_kwargs: Any) -> None:
        answers.append(text)

    class FakeAccess:
        async def get_membership_status(self, user_id: int) -> dict[str, Any]:
            return {
                "group_id": -100123,
                "user_id": user_id,
                "status": "member",
                "is_member": None,
                "allowed": True,
                "cache_hit": False,
                "error": None,
                "admin_bypass": False,
            }

    monkeypatch.setattr(Message, "answer", fake_answer)
    settings = settings_with_group(tmp_path, admin_ids={ADMIN_ID})

    asyncio.run(cmd_access_check(make_message("/access_check 123", user_id=ADMIN_ID), settings, FakeAccess()))

    assert "allowed: yes" in answers[0]
    assert "raw status: member" in answers[0]


def test_access_check_denied_for_non_admin(monkeypatch: pytest.MonkeyPatch, tmp_path):
    answers: list[str] = []

    async def fake_answer(self: Message, text: str, **_kwargs: Any) -> None:
        answers.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    settings = settings_with_group(tmp_path, admin_ids={ADMIN_ID})

    asyncio.run(cmd_access_check(make_message("/access_check 123", user_id=999), settings, object()))  # type: ignore[arg-type]

    assert answers == ["Недостаточно прав."]


def test_denied_user_can_invoke_create_access_handler_for_access_screen(monkeypatch: pytest.MonkeyPatch, tmp_path):
    called = False

    class FakeAccess:
        async def get_membership_status(self, user_id: int) -> dict[str, Any]:
            return {"allowed": False}

    async def handler(_event: Message, _data: dict[str, Any]) -> None:
        nonlocal called
        called = True

    middleware = GroupAccessMiddleware()

    asyncio.run(
        middleware(
            handler,  # type: ignore[arg-type]
            make_message("/create_access"),
            {"access_control": FakeAccess(), "settings": settings_with_group(tmp_path)},
        )
    )

    assert called is True


def test_member_user_can_invoke_create_access(tmp_path):
    called = False

    class FakeAccess:
        async def get_membership_status(self, user_id: int) -> dict[str, Any]:
            return {"allowed": True}

    async def handler(_event: Message, _data: dict[str, Any]) -> None:
        nonlocal called
        called = True

    middleware = GroupAccessMiddleware()

    asyncio.run(
        middleware(
            handler,  # type: ignore[arg-type]
            make_message("/create_access"),
            {"access_control": FakeAccess(), "settings": settings_with_group(tmp_path)},
        )
    )

    assert called is True


def test_create_access_handler_responds_when_self_service_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path):
    answers: list[str] = []

    async def fake_answer(self: Message, text: str, **_kwargs: Any) -> None:
        answers.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    service = make_service(str(tmp_path / "bot.db"), FakeXUIService([make_reality_inbound(7)]))
    access = AccessControlService(FakeTelegramBot(SimpleNamespace(status="member")), service.settings)  # type: ignore[arg-type]
    auto_provision = AutoProvisionService(service.db, service.xui, service.settings, access)

    asyncio.run(
        cmd_create_access(
            make_message("/create_access", user_id=1452759621),
            service,
            service.settings,
            service.db,
            auto_provision,
        )
    )

    assert answers
    assert "Self-service создание доступа сейчас выключено" in answers[0]
    assert "/create_inbound_dry_run" in answers[0]


def test_group_chat_config_command_is_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path):
    answers: list[str] = []
    called = False

    async def fake_answer(self: Message, text: str, **_kwargs: Any) -> None:
        answers.append(text)

    async def handler(_event: Message, _data: dict[str, Any]) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(Message, "answer", fake_answer)
    middleware = GroupAccessMiddleware()

    asyncio.run(
        middleware(
            handler,  # type: ignore[arg-type]
            make_message("/configs", chat_type="supergroup", chat_id=-10042),
            {"settings": settings_with_group(tmp_path)},
        )
    )

    assert called is False
    assert answers == ["Для работы с конфигурациями напишите боту в личные сообщения."]


def test_group_chat_config_tile_is_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path):
    answers: list[str] = []
    called = False

    async def fake_answer(self: Message, text: str, **_kwargs: Any) -> None:
        answers.append(text)

    async def handler(_event: Message, _data: dict[str, Any]) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(Message, "answer", fake_answer)
    middleware = GroupAccessMiddleware()

    asyncio.run(
        middleware(
            handler,  # type: ignore[arg-type]
            make_message(BTN_CONFIGS, chat_type="supergroup", chat_id=-10042),
            {"settings": settings_with_group(tmp_path)},
        )
    )

    assert called is False
    assert answers == ["Для работы с конфигурациями напишите боту в личные сообщения."]


def test_menu_text_maps_to_command():
    assert menu_text_to_command(BTN_CONFIGS) == "/configs"
    assert menu_text_to_command(BTN_INSTRUCTION) == "/instruction"
    assert menu_text_to_command("unknown") is None


def test_admin_menu_contains_admin_tiles_only_for_admin():
    user_keyboard = main_menu_keyboard(is_admin=False)
    admin_keyboard = main_menu_keyboard(is_admin=True)
    user_buttons = {button.text for row in user_keyboard.keyboard for button in row}
    admin_buttons = {button.text for row in admin_keyboard.keyboard for button in row}

    assert BTN_API_CHECK not in user_buttons
    assert BTN_API_CHECK in admin_buttons
    assert BTN_INSTRUCTION in user_buttons
    assert BTN_INSTRUCTION in admin_buttons
