from __future__ import annotations

import asyncio
import copy
from typing import Any

from bot.xui_client import (
    XUIService,
    compare_inbound_immutable_fields,
    extract_clients,
)
from tests.fakes import (
    ADMIN_ID,
    FakeXUIService,
    make_client,
    make_reality_inbound,
    make_service,
)


OPENAPI_330 = {
    "paths": {
        "/panel/api/inbounds/list": {"get": {}},
        "/panel/api/inbounds/get/{id}": {"get": {}},
        "/panel/api/clients/add": {"post": {}},
        "/panel/api/clients/update/{email}": {"post": {}},
    }
}


class RecordingXUIService(XUIService):
    def __init__(self, inbound: dict[str, Any]) -> None:
        self.inbound = copy.deepcopy(inbound)
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def get_inbound(self, inbound_id: int) -> dict[str, Any] | None:
        if int(self.inbound["id"]) != inbound_id:
            return None
        return copy.deepcopy(self.inbound)

    async def get_openapi(self) -> dict[str, Any]:
        return OPENAPI_330

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.requests.append((method, path, kwargs))
        if path == "/panel/api/clients/add":
            payload = kwargs["json"]
            self.inbound["settings"]["clients"].append(payload["client"])
        return {}


def test_xui_add_client_uses_3xui_330_clients_add_endpoint_without_full_inbound_payload():
    inbound = make_reality_inbound(7, clients=[make_client("old-uuid", "external")])
    xui = RecordingXUIService(inbound)

    asyncio.run(
        xui.add_client(
            inbound_id=7,
            telegram_id=123456789,
            title="phone",
            client_uuid="new-uuid",
            email="tg_123456789_01",
            expires_at=None,
            traffic_gb=0,
        )
    )

    assert len(xui.requests) == 1
    method, path, kwargs = xui.requests[0]
    assert method == "POST"
    assert path == "/panel/api/clients/add"
    assert set(kwargs["json"]) == {"client", "inboundIds"}
    assert kwargs["json"]["inboundIds"] == [7]
    assert "port" not in kwargs["json"]
    assert "protocol" not in kwargs["json"]
    assert "streamSettings" not in kwargs["json"]
    assert kwargs["json"]["client"]["email"] == "tg_123456789_01"
    assert kwargs["json"]["client"]["tgId"] == 123456789
    assert isinstance(kwargs["json"]["client"]["tgId"], int)


def test_add_client_does_not_change_port_protocol_or_stream_settings(tmp_path):
    inbound = make_reality_inbound(7, clients=[make_client("old-uuid", "external")])
    fake_xui = FakeXUIService([inbound])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    asyncio.run(service.bind_user(ADMIN_ID, 123456789, 7, 30))
    before = fake_xui.snapshot()[7]

    asyncio.run(service.create_config(123456789, "phone"))
    after = fake_xui.snapshot()[7]

    assert before["port"] == after["port"]
    assert before["protocol"] == after["protocol"]
    assert before["streamSettings"] == after["streamSettings"]
    assert compare_inbound_immutable_fields(before, after) == []


def test_add_client_preserves_existing_clients_and_adds_one(tmp_path):
    existing = [
        make_client("old-uuid-1", "external-1"),
        make_client("old-uuid-2", "external-2"),
    ]
    fake_xui = FakeXUIService([make_reality_inbound(7, clients=existing)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    asyncio.run(service.bind_user(ADMIN_ID, 123456789, 7, 30))

    asyncio.run(service.create_config(123456789, "phone"))

    clients = extract_clients(fake_xui.snapshot()[7])
    assert len(clients) == 3
    assert {client["email"] for client in clients} >= {"external-1", "external-2", "tg_123456789_01"}


def test_disable_user_clients_only_disables_matching_email_prefix():
    clients = [
        make_client("uuid-1", "tg_123456789_01", enabled=True),
        make_client("uuid-2", "tg_123456789_02", enabled=True),
        make_client("uuid-3", "external", enabled=True, tg_id=123456789),
        make_client("uuid-4", "tg_987654321_01", enabled=True),
    ]
    fake_xui = FakeXUIService([make_reality_inbound(7, clients=clients)])

    asyncio.run(fake_xui.disable_user_clients(7, 123456789))

    by_email = {client["email"]: client for client in extract_clients(fake_xui.snapshot()[7])}
    assert by_email["tg_123456789_01"]["enable"] is False
    assert by_email["tg_123456789_02"]["enable"] is False
    assert by_email["external"]["enable"] is True
    assert by_email["tg_987654321_01"]["enable"] is True


def test_bind_does_not_call_mutate_endpoints(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)

    asyncio.run(service.bind_user(ADMIN_ID, 123456789, 7, 30))

    assert fake_xui.mutate_calls == []


def test_new_config_calls_only_client_add_mutate_endpoint(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    asyncio.run(service.bind_user(ADMIN_ID, 123456789, 7, 30))

    asyncio.run(service.create_config(123456789, "phone"))

    assert fake_xui.mutate_calls == ["addClient"]


def test_bind_dry_run_does_not_mutate_xui_or_sqlite(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)

    report = asyncio.run(service.bind_dry_run(ADMIN_ID, 123456789, 7, 30))

    assert report.would_create_user is True
    assert service.db.get_user(123456789) is None
    assert fake_xui.mutate_calls == []


def test_new_config_dry_run_does_not_mutate_xui_or_sqlite(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    asyncio.run(service.bind_user(ADMIN_ID, 123456789, 7, 30))
    fake_xui.mutate_calls.clear()

    report = asyncio.run(service.new_config_dry_run(ADMIN_ID, 123456789, "phone"))

    assert report.email == "tg_123456789_01"
    assert service.db.list_configs(123456789) == []
    assert fake_xui.mutate_calls == []


def test_create_config_audits_critical_if_immutable_field_changes(tmp_path):
    class MutatingFakeXUIService(FakeXUIService):
        async def add_client(self, *args: Any, **kwargs: Any) -> None:
            await super().add_client(*args, **kwargs)
            inbound_id = int(kwargs.get("inbound_id", args[0] if args else 7))
            self.inbounds[inbound_id]["port"] = 8443

    fake_xui = MutatingFakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    asyncio.run(service.bind_user(ADMIN_ID, 123456789, 7, 30))

    created = asyncio.run(service.create_config(123456789, "phone"))

    assert created.immutable_changes == ["port"]
    assert any(row["action"] == "critical_inbound_immutable_changed" for row in service.db.audit_entries())


def test_create_config_audits_critical_if_existing_clients_disappear(tmp_path):
    class ClientDroppingFakeXUIService(FakeXUIService):
        async def add_client(self, *args: Any, **kwargs: Any) -> None:
            await super().add_client(*args, **kwargs)
            inbound_id = int(kwargs.get("inbound_id", args[0] if args else 7))
            self.inbounds[inbound_id]["settings"]["clients"] = self.inbounds[inbound_id]["settings"]["clients"][-1:]

    fake_xui = ClientDroppingFakeXUIService(
        [make_reality_inbound(7, clients=[make_client("old-uuid", "external")])]
    )
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    asyncio.run(service.bind_user(ADMIN_ID, 123456789, 7, 30))

    created = asyncio.run(service.create_config(123456789, "phone"))

    assert created.client_integrity_warnings == ["existing_clients_missing_after_add"]
    assert any(row["action"] == "critical_inbound_clients_changed" for row in service.db.audit_entries())
