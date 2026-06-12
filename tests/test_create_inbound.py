from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from typing import Any

import pytest

from bot.services import AlreadyBound, ServiceError, ValidationError
from bot.xui_client import XUIService, extract_clients
from tests.fakes import (
    ADMIN_ID,
    FakeXUIService,
    make_client,
    make_reality_inbound,
    make_service,
)


def test_create_inbound_dry_run_does_not_mutate_xui_or_sqlite(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)

    report = asyncio.run(
        service.create_inbound_dry_run(
            actor_tg_id=ADMIN_ID,
            template_inbound_id=7,
            target_tg_id=1452759621,
            days=30,
            requested_port="auto",
            remark="client-1452759621",
        )
    )

    assert report.template_inbound_id == 7
    assert report.target_tg_id == 1452759621
    assert report.port != 443
    assert fake_xui.mutate_calls == []
    assert service.db.get_user(1452759621) is None


def test_create_inbound_refuses_used_port(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)

    with pytest.raises(ValidationError, match="already used"):
        asyncio.run(
            service.create_inbound_dry_run(
                actor_tg_id=ADMIN_ID,
                template_inbound_id=7,
                target_tg_id=1452759621,
                days=30,
                requested_port="443",
                remark="client-1452759621",
            )
        )


def test_create_inbound_refuses_already_bound_user(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7), make_reality_inbound(8)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    asyncio.run(service.bind_user(ADMIN_ID, 1452759621, 7, 30))

    with pytest.raises(AlreadyBound):
        asyncio.run(
            service.create_inbound_dry_run(
                actor_tg_id=ADMIN_ID,
                template_inbound_id=8,
                target_tg_id=1452759621,
                days=30,
                requested_port="auto",
                remark="client-1452759621",
            )
        )


def test_create_inbound_from_template_clears_clients_and_binds_user(tmp_path):
    template = make_reality_inbound(
        7,
        clients=[
            make_client("old-uuid-1", "external-1"),
            make_client("old-uuid-2", "external-2"),
        ],
    )
    fake_xui = FakeXUIService([template])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)

    result = asyncio.run(
        service.create_inbound_from_template(
            actor_tg_id=ADMIN_ID,
            template_inbound_id=7,
            target_tg_id=1452759621,
            days=30,
            requested_port="auto",
            remark="client-1452759621",
        )
    )

    assert result.inbound_id != 7
    assert result.clone_warnings == []
    assert fake_xui.mutate_calls == ["addInbound"]
    user = service.db.get_user(1452759621)
    assert user is not None
    assert user.inbound_id == result.inbound_id
    created = fake_xui.inbounds[result.inbound_id]
    assert extract_clients(created) == []
    assert created["streamSettings"] == template["streamSettings"]
    assert created["sniffing"] == template.get("sniffing", {})


def test_self_service_access_is_disabled_by_default(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui, admin_ids={ADMIN_ID})

    with pytest.raises(ServiceError, match="disabled"):
        asyncio.run(service.create_self_service_access(1452759621))

    assert fake_xui.mutate_calls == []
    assert service.db.get_user(1452759621) is None


def test_self_service_access_creates_inbound_from_template_without_admin(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui, admin_ids={ADMIN_ID})
    service.settings = replace(
        service.settings,
        self_service_create_access=True,
        self_service_template_inbound_id=7,
    )

    result = asyncio.run(service.create_self_service_access(1452759621))

    assert result.template_inbound_id == 7
    assert result.target_tg_id == 1452759621
    assert result.inbound_id != 7
    assert result.remark == "tg_1452759621"
    assert fake_xui.mutate_calls == ["addInbound"]
    assert service.db.get_user(1452759621).inbound_id == result.inbound_id
    assert extract_clients(fake_xui.inbounds[result.inbound_id]) == []


class RecordingCreateInboundXUI(XUIService):
    def __init__(self, template: dict[str, Any]) -> None:
        self.template = copy.deepcopy(template)
        self.inbounds = {int(template["id"]): copy.deepcopy(template)}
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def get_inbound(self, inbound_id: int) -> dict[str, Any] | None:
        inbound = self.inbounds.get(inbound_id)
        return copy.deepcopy(inbound) if inbound is not None else None

    async def list_inbounds(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(inbound) for inbound in self.inbounds.values()]

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.requests.append((method, path, kwargs))
        payload = kwargs["json"]
        created = {
            **payload,
            "id": 99,
            "tag": f"in-{payload['port']}",
        }
        self.inbounds[99] = created
        return {"id": 99}


def test_xui_create_inbound_uses_add_endpoint_and_omits_template_clients():
    template = make_reality_inbound(
        7,
        clients=[make_client("old-uuid", "external")],
    )
    xui = RecordingCreateInboundXUI(template)

    created = asyncio.run(
        xui.create_inbound_from_template(
            template_inbound_id=7,
            port=24443,
            remark="client-1452759621",
        )
    )

    assert created["id"] == 99
    assert len(xui.requests) == 1
    method, path, kwargs = xui.requests[0]
    assert method == "POST"
    assert path == "/panel/api/inbounds/add"
    payload = kwargs["json"]
    assert "id" not in payload
    assert "tag" not in payload
    assert "clientStats" not in payload
    assert payload["port"] == 24443
    assert payload["remark"] == "client-1452759621"
    assert payload["settings"]["clients"] == []
    assert payload["streamSettings"] == template["streamSettings"]
