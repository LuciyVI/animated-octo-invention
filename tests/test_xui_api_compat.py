from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bot.xui_client import XUIService
from scripts import smoke_real_xui_readonly
from tests.fakes import (
    ADMIN_ID,
    FakeXUIService,
    make_client,
    make_reality_inbound,
    make_service,
)


FULL_OPENAPI = {
    "openapi": "3.0.0",
    "info": {"title": "3x-ui", "version": "3.3.0"},
    "paths": {
        "/panel/api/inbounds/list": {"get": {}},
        "/panel/api/inbounds/get/{id}": {"get": {}},
        "/panel/api/inbounds/add": {"post": {}},
        "/panel/api/inbounds/update/{id}": {"post": {}},
        "/panel/api/clients/add": {"post": {}},
        "/panel/api/clients/update/{email}": {"post": {}},
    },
}


class ApiCheckStub(XUIService):
    def __init__(
        self,
        openapi: dict[str, Any] | Exception,
        inbounds: list[dict[str, Any]] | Exception,
    ) -> None:
        self.openapi = openapi
        self.inbounds = inbounds

    async def get_openapi(self) -> dict[str, Any]:
        if isinstance(self.openapi, Exception):
            raise self.openapi
        return self.openapi

    async def list_inbounds(self) -> list[dict[str, Any]]:
        if isinstance(self.inbounds, Exception):
            raise self.inbounds
        return self.inbounds


class StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.response = type("Response", (), {"status_code": status_code})()
        super().__init__(f"HTTP {status_code}")


def test_bearer_token_added_to_headers():
    service = XUIService("http://127.0.0.1:54321/", "secret-token")
    try:
        assert service._client.headers["Authorization"] == "Bearer secret-token"
        assert "X-API-Key" not in service._client.headers
    finally:
        asyncio.run(service.close())


def test_url_join_with_root_panel_path():
    service = XUIService("http://127.0.0.1:54321/", "token")
    try:
        assert service._url("/panel/api/inbounds/list") == "http://127.0.0.1:54321/panel/api/inbounds/list"
    finally:
        asyncio.run(service.close())


def test_url_join_with_secret_panel_path():
    service = XUIService("http://127.0.0.1:54321/secret-path/", "token")
    try:
        assert (
            service._url("/panel/api/inbounds/list")
            == "http://127.0.0.1:54321/secret-path/panel/api/inbounds/list"
        )
    finally:
        asyncio.run(service.close())


def test_api_check_detects_missing_endpoint():
    openapi = {
        "openapi": "3.0.0",
        "paths": {
            "/panel/api/inbounds/list": {"get": {}},
            "/panel/api/inbounds/get/{id}": {"get": {}},
            "/panel/api/inbounds/add": {"post": {}},
            "/panel/api/inbounds/update/{id}": {"post": {}},
            "/panel/api/clients/add": {"post": {}},
        },
    }
    service = ApiCheckStub(openapi, [make_reality_inbound(7)])

    report = asyncio.run(service.api_check())

    assert report["ok"] is False
    assert "POST client update endpoint" in report["missing_endpoints"]


def test_api_check_accepts_3xui_330_clients_endpoints():
    service = ApiCheckStub(FULL_OPENAPI, [make_reality_inbound(7)])

    report = asyncio.run(service.api_check())

    assert report["ok"] is True
    assert report["client_add_endpoint"] == "/panel/api/clients/add"
    assert report["client_update_endpoint"] == "/panel/api/clients/update/{email}"


def test_api_check_accepts_legacy_inbounds_client_endpoints():
    openapi = {
        "openapi": "3.0.0",
        "paths": {
            "/panel/api/inbounds/list": {"get": {}},
            "/panel/api/inbounds/get/{id}": {"get": {}},
            "/panel/api/inbounds/add": {"post": {}},
            "/panel/api/inbounds/update/{id}": {"post": {}},
            "/panel/api/inbounds/addClient": {"post": {}},
            "/panel/api/inbounds/updateClient/{clientId}": {"post": {}},
        },
    }
    service = ApiCheckStub(openapi, [make_reality_inbound(7)])

    report = asyncio.run(service.api_check())

    assert report["ok"] is True
    assert report["client_add_endpoint"] == "/panel/api/inbounds/addClient"
    assert report["client_update_endpoint"] == "/panel/api/inbounds/updateClient/{uuid}"


def test_api_check_returns_auth_error_for_401_403():
    service = ApiCheckStub(StatusError(401), StatusError(403))

    report = asyncio.run(service.api_check())

    assert report["ok"] is False
    assert report["auth_ok"] is False
    assert any("HTTP 401" in error for error in report["errors"])
    assert any("HTTP 403" in error for error in report["errors"])


def test_smoke_script_does_not_print_xui_token(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    token = "super-secret-xui-token"

    class SmokeFakeXUI:
        def __init__(self, host: str, token_value: str) -> None:
            assert token_value == token

        async def api_check(self) -> dict[str, Any]:
            return {
                "ok": True,
                "openapi_ok": True,
                "auth_ok": True,
                "inbounds_ok": True,
                "inbounds_count": 1,
                "client_add_ok": True,
                "client_update_ok": True,
                "client_add_endpoint": "/panel/api/clients/add",
                "client_update_endpoint": "/panel/api/clients/update/{email}",
                "missing_endpoints": [],
                "errors": [],
                "required_endpoints": {},
            }

        async def get_openapi(self) -> dict[str, Any]:
            return FULL_OPENAPI

        async def list_inbounds(self) -> list[dict[str, Any]]:
            return [make_reality_inbound(7)]

        async def close(self) -> None:
            return None

    monkeypatch.setenv("XUI_HOST", "http://127.0.0.1:54321/")
    monkeypatch.setenv("XUI_TOKEN", token)
    monkeypatch.setattr(smoke_real_xui_readonly, "XUIService", SmokeFakeXUI)

    exit_code = asyncio.run(smoke_real_xui_readonly.main())
    output = capsys.readouterr().out

    assert exit_code == 0
    assert token not in output
    assert "api_check: ok" in output


def test_list_inbounds_uses_read_only_endpoint():
    class RecordingReadOnlyXUI(XUIService):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
            self.calls.append((method, path))
            return []

    service = RecordingReadOnlyXUI()

    asyncio.run(service.list_inbounds())

    assert service.calls == [("GET", "/panel/api/inbounds/list")]


def test_new_config_uses_only_add_client_mutate_call(tmp_path):
    fake_xui = FakeXUIService([make_reality_inbound(7)])
    service = make_service(str(tmp_path / "bot.db"), fake_xui)
    asyncio.run(service.bind_user(ADMIN_ID, 123456789, 7, 30))

    asyncio.run(service.create_config(123456789, "phone"))

    assert fake_xui.mutate_calls == ["addClient"]


def test_disable_uses_3xui_330_update_client_and_does_not_send_inbound_settings():
    class RecordingUpdateXUI(XUIService):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any]]] = []

        async def get_inbound(self, inbound_id: int) -> dict[str, Any] | None:
            return make_reality_inbound(
                inbound_id,
                clients=[make_client("uuid-1", "tg_123456789_01", enabled=True)],
            )

        async def get_openapi(self) -> dict[str, Any]:
            return FULL_OPENAPI

        async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
            self.calls.append((method, path, kwargs))
            return {}

    service = RecordingUpdateXUI()

    asyncio.run(service.disable_client(7, "uuid-1"))

    assert len(service.calls) == 1
    method, path, kwargs = service.calls[0]
    assert method == "POST"
    assert path == "/panel/api/clients/update/tg_123456789_01"
    assert kwargs["json"]["email"] == "tg_123456789_01"
    assert kwargs["json"]["enable"] is False
    assert "streamSettings" not in kwargs["json"]
    assert "port" not in kwargs["json"]
