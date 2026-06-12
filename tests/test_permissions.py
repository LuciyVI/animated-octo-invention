from __future__ import annotations

import asyncio

import pytest

from bot.services import PermissionDenied
from bot.utils.security import is_admin_id
from tests.fakes import ADMIN_ID, FakeXUIService, make_reality_inbound, make_service


def test_non_admin_cannot_bind(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui, admin_ids={ADMIN_ID})

    with pytest.raises(PermissionDenied):
        asyncio.run(service.bind_user(222222222, 123456789, 7, 30))

    assert service.db.get_user(123456789) is None


def test_is_admin_id_uses_numeric_telegram_id_only():
    assert is_admin_id(ADMIN_ID, {ADMIN_ID})
    assert not is_admin_id(222222222, {ADMIN_ID})
    assert not is_admin_id(None, {ADMIN_ID})

