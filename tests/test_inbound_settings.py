from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from bot.services import PermissionDenied
from bot.xui_client import extract_clients
from tests.fakes import ADMIN_ID, FakeXUIService, make_client, make_db, make_reality_inbound, make_settings
from bot.services import BotService


def _service_with_update_flag(tmp_path, fake_xui: FakeXUIService, enabled: bool) -> BotService:
    settings = replace(
        make_settings(str(tmp_path / "bot.db")),
        user_can_change_inbound_core_settings=enabled,
    )
    return BotService(make_db(settings.db_path), fake_xui, settings)


def test_own_inbound_update_is_blocked_when_flag_disabled(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = _service_with_update_flag(tmp_path, fake_xui, enabled=False)
    asyncio.run(service.bind_user(ADMIN_ID, 123456789, 7, 30))

    with pytest.raises(PermissionDenied):
        asyncio.run(service.update_own_inbound_from_payload(123456789, {"remark": "new"}))

    assert "updateInbound" not in fake_xui.mutate_calls


def test_own_inbound_update_preserves_clients(tmp_path):
    clients = [make_client("uuid-1", "tg_123456789_01", enabled=True)]
    fake_xui = FakeXUIService([make_reality_inbound(7, clients=clients)])
    service = _service_with_update_flag(tmp_path, fake_xui, enabled=True)
    asyncio.run(service.bind_user(ADMIN_ID, 123456789, 7, 30))

    result = asyncio.run(
        service.update_own_inbound_from_payload(
            123456789,
            {
                "remark": "custom-remark",
                "settings": {"clients": []},
            },
        )
    )

    inbound = fake_xui.snapshot()[7]
    assert result.changed_fields == ["remark"]
    assert inbound["remark"] == "custom-remark"
    assert [client["email"] for client in extract_clients(inbound)] == ["tg_123456789_01"]
    assert fake_xui.mutate_calls[-1] == "updateInbound"
