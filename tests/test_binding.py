from __future__ import annotations

import asyncio

import pytest

from bot.services import AlreadyBound
from tests.fakes import ADMIN_ID, FakeXUIService, make_reality_inbound, make_service


def test_bind_does_not_create_inbound(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)

    before_ids = set(fake_xui.inbounds)
    asyncio.run(service.bind_user(ADMIN_ID, 123456789, 7, 30))

    assert set(fake_xui.inbounds) == before_ids
    assert fake_xui.add_client_calls == 0
    assert service.db.get_user(123456789).inbound_id == 7


def test_bind_does_not_change_inbound_settings(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    before = fake_xui.snapshot()

    asyncio.run(service.bind_user(ADMIN_ID, 123456789, 7, 30))

    assert fake_xui.snapshot() == before


def test_cannot_bind_one_inbound_to_two_telegram_ids(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)

    asyncio.run(service.bind_user(ADMIN_ID, 111, 7, 30))

    with pytest.raises(AlreadyBound):
        asyncio.run(service.bind_user(ADMIN_ID, 222, 7, 30))


def test_cannot_bind_one_telegram_id_to_two_inbounds(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7), make_reality_inbound(8)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)

    asyncio.run(service.bind_user(ADMIN_ID, 111, 7, 30))

    with pytest.raises(AlreadyBound):
        asyncio.run(service.bind_user(ADMIN_ID, 111, 8, 30))


def test_sync_marks_user_orphaned_when_inbound_missing(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    service.db.upsert_user(123456789, 99, "active", None, 5)

    report = asyncio.run(service.sync(ADMIN_ID))

    assert report.orphaned == [123456789]
    assert service.db.get_user(123456789).status == "orphaned"

