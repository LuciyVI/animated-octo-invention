from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from bot.access_control import AccessControlService
from bot.auto_provision import AutoProvisionService
from bot.xui_client import extract_clients
from tests.fakes import FakeXUIService, make_client, make_reality_inbound, make_service


class FakeTelegramBot:
    def __init__(self, status: str = "member") -> None:
        self.status = status

    async def get_chat_member(self, chat_id: int, user_id: int) -> object:
        return SimpleNamespace(status=self.status, is_member=None)


def make_auto_service(tmp_path, fake_xui: FakeXUIService, *, member_status: str = "member", auto: bool = True):
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    service.settings = replace(
        service.settings,
        require_group_membership=True,
        access_group_id=-1001234567890,
        auto_provision_inbound=auto,
        template_inbound_remark="Moroz",
        template_inbound_id=None,
        port_min=30000,
        port_max=30010,
        default_access_days=30,
        max_configs_per_inbound=5,
    )
    access = AccessControlService(FakeTelegramBot(member_status), service.settings)  # type: ignore[arg-type]
    return service, AutoProvisionService(service.db, fake_xui, service.settings, access)


def moroz_inbound(inbound_id: int = 15, clients=None, port: int = 10429):
    inbound = make_reality_inbound(inbound_id, clients=clients, protocol="vless")
    inbound["remark"] = "Moroz"
    inbound["port"] = port
    return inbound


def test_non_group_user_does_not_get_inbound(tmp_path):
    fake_xui = FakeXUIService([moroz_inbound()])
    service, auto = make_auto_service(tmp_path, fake_xui, member_status="left")

    result = asyncio.run(auto.ensure_user_access(1452759621))

    assert result.allowed is False
    assert service.db.get_user(1452759621) is None
    assert fake_xui.mutate_calls == []


def test_group_user_gets_inbound_automatically(tmp_path):
    template = moroz_inbound(clients=[make_client("old", "external")])
    fake_xui = FakeXUIService([template])
    service, auto = make_auto_service(tmp_path, fake_xui)
    before_template = fake_xui.snapshot()[15]

    result = asyncio.run(auto.ensure_user_access(1452759621))

    assert result.allowed is True
    assert result.created is True
    assert result.inbound_id is not None
    user = service.db.get_user(1452759621)
    assert user is not None
    assert user.inbound_id == result.inbound_id
    assert user.status == "active"
    assert user.created_by == "auto_group"
    assert user.access_source == "telegram_group"
    created = fake_xui.inbounds[result.inbound_id]
    assert created["remark"] == "tg_1452759621"
    assert created["port"] == 30000
    assert extract_clients(created) == []
    assert fake_xui.inbounds[15] == before_template


def test_repeated_start_does_not_create_second_inbound(tmp_path):
    fake_xui = FakeXUIService([moroz_inbound()])
    _service, auto = make_auto_service(tmp_path, fake_xui)

    first = asyncio.run(auto.ensure_user_access(1452759621))
    second = asyncio.run(auto.ensure_user_access(1452759621))

    assert first.inbound_id == second.inbound_id
    assert fake_xui.mutate_calls == ["addInbound"]


def test_fast_repeated_calls_do_not_create_second_inbound(tmp_path):
    fake_xui = FakeXUIService([moroz_inbound()])
    _service, auto = make_auto_service(tmp_path, fake_xui)

    async def run_twice():
        return await asyncio.gather(
            auto.ensure_user_access(1452759621),
            auto.ensure_user_access(1452759621),
        )

    results = asyncio.run(run_twice())

    assert {result.inbound_id for result in results} == {results[0].inbound_id}
    assert fake_xui.mutate_calls == ["addInbound"]


def test_template_moroz_exact_remark_is_required(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(15)])
    service, auto = make_auto_service(tmp_path, fake_xui)

    result = asyncio.run(auto.ensure_user_access(1452759621))

    assert result.status == "failed"
    assert service.db.get_user(1452759621).status == "pending"
    assert fake_xui.mutate_calls == []


def test_duplicate_template_moroz_refuses_to_create(tmp_path):
    fake_xui = FakeXUIService([moroz_inbound(15), moroz_inbound(16, port=10430)])
    service, auto = make_auto_service(tmp_path, fake_xui)

    result = asyncio.run(auto.ensure_user_access(1452759621))

    assert result.status == "failed"
    assert service.db.get_user(1452759621).status == "pending"
    assert fake_xui.mutate_calls == []


def test_port_skips_local_and_remote_allocations(tmp_path):
    remote = make_reality_inbound(20)
    remote["remark"] = "occupied"
    remote["port"] = 30001
    fake_xui = FakeXUIService([moroz_inbound(), remote])
    service, auto = make_auto_service(tmp_path, fake_xui)
    assert service.db.reserve_port(30000, 999)

    result = asyncio.run(auto.ensure_user_access(1452759621))

    assert result.port == 30002


def test_auto_provision_disabled_does_not_create(tmp_path):
    fake_xui = FakeXUIService([moroz_inbound()])
    service, auto = make_auto_service(tmp_path, fake_xui, auto=False)

    result = asyncio.run(auto.ensure_user_access(1452759621))

    assert result.status == "not_provisioned"
    assert service.db.get_user(1452759621) is None
    assert fake_xui.mutate_calls == []
