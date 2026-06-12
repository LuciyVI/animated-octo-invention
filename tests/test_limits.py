from __future__ import annotations

import asyncio

import pytest

from bot.services import AccessDenied, LimitExceeded
from tests.fakes import (
    ADMIN_ID,
    FakeXUIService,
    make_client,
    make_reality_inbound,
    make_service,
)


def test_cannot_create_more_than_five_configs_per_inbound(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    tg_id = 123456789
    asyncio.run(service.bind_user(ADMIN_ID, tg_id, 7, 30))

    for index in range(5):
        created = asyncio.run(service.create_config(tg_id, title=f"device-{index + 1}"))
        assert created.config.email == f"tg_{tg_id}_{index + 1:02d}"

    with pytest.raises(LimitExceeded):
        asyncio.run(service.create_config(tg_id, title="sixth"))


def test_existing_clients_in_inbound_are_counted_in_limit(tmp_path):
    existing = [
        make_client(f"uuid-{idx}", f"external-{idx}", enabled=True)
        for idx in range(5)
    ]
    fake_xui = FakeXUIService([make_reality_inbound(7, clients=existing)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    tg_id = 123456789
    asyncio.run(service.bind_user(ADMIN_ID, tg_id, 7, 30))

    with pytest.raises(LimitExceeded):
        asyncio.run(service.create_config(tg_id, title="phone"))


def test_disabled_user_cannot_create_new_config(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    tg_id = 123456789
    asyncio.run(service.bind_user(ADMIN_ID, tg_id, 7, 30))
    asyncio.run(service.disable_user(ADMIN_ID, tg_id))

    with pytest.raises(AccessDenied):
        asyncio.run(service.create_config(tg_id, title="phone"))

