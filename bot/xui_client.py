from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any
from urllib.parse import quote

try:
    import httpx
except ImportError:  # pragma: no cover - exercised only without optional deps
    httpx = None  # type: ignore[assignment]


class XUIError(RuntimeError):
    pass


REQUIRED_API_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("GET", "/panel/api/inbounds/list"),
    ("GET", "/panel/api/inbounds/get/{id}"),
)

CLIENT_ADD_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("POST", "/panel/api/clients/add"),
    ("POST", "/panel/api/inbounds/addClient"),
)

CLIENT_UPDATE_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("POST", "/panel/api/clients/update/{email}"),
    ("POST", "/panel/api/inbounds/updateClient/{uuid}"),
)

INBOUND_CREATE_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("POST", "/panel/api/inbounds/add"),
)

INBOUND_UPDATE_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("POST", "/panel/api/inbounds/update/{id}"),
)


def parse_json_field(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        if not value.strip():
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def extract_settings(inbound: dict[str, Any]) -> dict[str, Any]:
    settings = parse_json_field(inbound.get("settings"), {})
    return settings if isinstance(settings, dict) else {}


def extract_stream_settings(inbound: dict[str, Any]) -> dict[str, Any]:
    stream_settings = parse_json_field(inbound.get("streamSettings"), {})
    return stream_settings if isinstance(stream_settings, dict) else {}


def extract_sniffing(inbound: dict[str, Any]) -> dict[str, Any]:
    sniffing = parse_json_field(inbound.get("sniffing"), {})
    return sniffing if isinstance(sniffing, dict) else {}


def extract_clients(inbound: dict[str, Any]) -> list[dict[str, Any]]:
    settings = extract_settings(inbound)
    clients = settings.get("clients", [])
    if isinstance(clients, list):
        return [client for client in clients if isinstance(client, dict)]
    return []


def client_is_enabled(client: dict[str, Any]) -> bool:
    if "enable" in client:
        return bool(client["enable"])
    if "enabled" in client:
        return bool(client["enabled"])
    return True


def count_active_clients_in_inbound(inbound: dict[str, Any]) -> int:
    return sum(1 for client in extract_clients(inbound) if client_is_enabled(client))


def client_belongs_to_tg(client: dict[str, Any], telegram_id: int) -> bool:
    prefix = f"tg_{telegram_id}_"
    email = str(client.get("email", ""))
    return email.startswith(prefix)


def count_active_clients_for_tg(inbound: dict[str, Any], telegram_id: int) -> int:
    return sum(
        1
        for client in extract_clients(inbound)
        if client_belongs_to_tg(client, telegram_id) and client_is_enabled(client)
    )


def client_identity(client: dict[str, Any]) -> str:
    client_id = str(client.get("id") or "").strip()
    if client_id:
        return f"id:{client_id}"
    return f"email:{str(client.get('email') or '').strip()}"


def client_identity_set(inbound: dict[str, Any]) -> set[str]:
    return {identity for client in extract_clients(inbound) if (identity := client_identity(client)) != "email:"}


IMMUTABLE_INBOUND_FIELDS = (
    "id",
    "port",
    "protocol",
    "remark",
    "enable",
    "streamSettings",
    "sniffing",
    "listen",
    "tag",
)


def _canonical_json_value(value: Any) -> Any:
    parsed = parse_json_field(value, value)
    if isinstance(parsed, dict):
        return {str(key): _canonical_json_value(parsed[key]) for key in sorted(parsed)}
    if isinstance(parsed, list):
        return [_canonical_json_value(item) for item in parsed]
    return parsed


def inbound_immutable_snapshot(inbound: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": inbound.get("id"),
        "port": inbound.get("port"),
        "protocol": inbound.get("protocol"),
        "remark": inbound.get("remark"),
        "enable": inbound.get("enable", inbound.get("enabled")),
        "streamSettings": _canonical_json_value(inbound.get("streamSettings", {})),
        "sniffing": _canonical_json_value(inbound.get("sniffing", {})),
        "listen": inbound.get("listen"),
        "tag": inbound.get("tag"),
    }


def compare_inbound_immutable_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    before_snapshot = inbound_immutable_snapshot(before)
    after_snapshot = inbound_immutable_snapshot(after)
    return [
        field
        for field in IMMUTABLE_INBOUND_FIELDS
        if before_snapshot.get(field) != after_snapshot.get(field)
    ]


def build_inbound_payload_from_template(
    template: dict[str, Any],
    port: int,
    remark: str,
    tag: str | None = None,
) -> dict[str, Any]:
    settings = deepcopy(extract_settings(template))
    settings["clients"] = []
    payload = {
        "enable": bool(template.get("enable", template.get("enabled", True))),
        "remark": remark,
        "listen": template.get("listen") or "",
        "port": port,
        "protocol": template.get("protocol"),
        "expiryTime": int(template.get("expiryTime") or 0),
        "total": int(template.get("total") or 0),
        "settings": settings,
        "streamSettings": deepcopy(extract_stream_settings(template)),
        "sniffing": deepcopy(extract_sniffing(template)),
    }
    if tag:
        payload["tag"] = tag
    return {key: value for key, value in payload.items() if value is not None}


def build_inbound_update_payload(inbound: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "enable": bool(inbound.get("enable", inbound.get("enabled", True))),
        "remark": str(inbound.get("remark") or ""),
        "listen": inbound.get("listen") or "",
        "port": int(inbound.get("port") or 0),
        "protocol": inbound.get("protocol"),
        "expiryTime": int(inbound.get("expiryTime") or 0),
        "total": int(inbound.get("total") or 0),
        "settings": deepcopy(extract_settings(inbound)),
        "streamSettings": deepcopy(extract_stream_settings(inbound)),
        "sniffing": deepcopy(extract_sniffing(inbound)),
    }
    if "tag" in inbound:
        payload["tag"] = inbound.get("tag") or ""
    return {key: value for key, value in payload.items() if value is not None}


def compare_template_clone_fields(template: dict[str, Any], cloned: dict[str, Any]) -> list[str]:
    expected = build_inbound_payload_from_template(
        template,
        port=int(cloned.get("port") or 0),
        remark=str(cloned.get("remark") or ""),
    )
    actual = {
        "enable": bool(cloned.get("enable", cloned.get("enabled", True))),
        "remark": cloned.get("remark"),
        "listen": cloned.get("listen") or "",
        "port": cloned.get("port"),
        "protocol": cloned.get("protocol"),
        "expiryTime": int(cloned.get("expiryTime") or 0),
        "total": int(cloned.get("total") or 0),
        "settings": _canonical_json_value(extract_settings(cloned) | {"clients": []}),
        "streamSettings": _canonical_json_value(extract_stream_settings(cloned)),
        "sniffing": _canonical_json_value(extract_sniffing(cloned)),
    }
    comparable_expected = {
        **expected,
        "settings": _canonical_json_value(expected.get("settings", {})),
        "streamSettings": _canonical_json_value(expected.get("streamSettings", {})),
        "sniffing": _canonical_json_value(expected.get("sniffing", {})),
    }
    return [
        field
        for field, expected_value in comparable_expected.items()
        if actual.get(field) != expected_value
    ]


def compare_template_clone_core_fields(template: dict[str, Any], cloned: dict[str, Any]) -> list[str]:
    expected = {
        "protocol": template.get("protocol"),
        "streamSettings": _canonical_json_value(extract_stream_settings(template)),
        "sniffing": _canonical_json_value(extract_sniffing(template)),
        "listen": template.get("listen") or "",
        "settings": _canonical_json_value(extract_settings(template) | {"clients": []}),
    }
    actual = {
        "protocol": cloned.get("protocol"),
        "streamSettings": _canonical_json_value(extract_stream_settings(cloned)),
        "sniffing": _canonical_json_value(extract_sniffing(cloned)),
        "listen": cloned.get("listen") or "",
        "settings": _canonical_json_value(extract_settings(cloned) | {"clients": []}),
    }
    return [field for field, expected_value in expected.items() if actual.get(field) != expected_value]


def normalize_xui_host(host: str) -> str:
    return host.strip().rstrip("/")


def normalize_openapi_path(path: str) -> str:
    return re.sub(r"\{[^}/]+\}", "{}", path.rstrip("/"))


def openapi_has_endpoint(openapi: dict[str, Any], method: str, path: str) -> bool:
    paths = openapi.get("paths")
    if not isinstance(paths, dict):
        return False
    wanted = normalize_openapi_path(path)
    wanted_method = method.lower()
    for candidate_path, operations in paths.items():
        if normalize_openapi_path(str(candidate_path)) != wanted:
            continue
        if isinstance(operations, dict) and wanted_method in {str(key).lower() for key in operations}:
            return True
    return False


class XUIService:
    def __init__(self, host: str, token: str, timeout: float = 20.0) -> None:
        if httpx is None:
            raise RuntimeError("httpx is required for XUIService")
        self.host = normalize_xui_host(host)
        self.token = token
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout, headers=self._headers())
        self._openapi_cache: dict[str, Any] | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

    def _url(self, path: str) -> str:
        return f"{self.host}/{path.lstrip('/')}"

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._client.request(method, self._url(path), **kwargs)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("success") is False:
            message = data.get("msg") or data.get("message") or "3x-ui API error"
            raise XUIError(str(message))
        if isinstance(data, dict) and "obj" in data:
            return data["obj"]
        return data

    async def _request_json_raw(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._client.request(method, self._url(path), **kwargs)
        response.raise_for_status()
        return response.json()

    async def healthcheck(self) -> bool:
        try:
            await self.list_inbounds()
            return True
        except Exception:
            return False

    async def get_openapi(self) -> dict[str, Any]:
        if self._openapi_cache is not None:
            return self._openapi_cache
        data = await self._request_json_raw("GET", "/panel/api/openapi.json")
        if not isinstance(data, dict):
            raise XUIError("OpenAPI response is not a JSON object")
        self._openapi_cache = data
        return data

    async def _endpoint_supported(self, method: str, path: str) -> bool:
        try:
            openapi = await self.get_openapi()
        except Exception:
            return False
        return openapi_has_endpoint(openapi, method, path)

    async def _choose_endpoint(
        self,
        candidates: tuple[tuple[str, str], ...],
        fallback: tuple[str, str],
    ) -> tuple[str, str]:
        try:
            openapi = await self.get_openapi()
        except Exception:
            return fallback
        for method, path in candidates:
            if openapi_has_endpoint(openapi, method, path):
                return method, path
        return fallback

    async def api_check(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "openapi_ok": False,
            "auth_ok": False,
            "inbounds_ok": False,
            "inbounds_count": 0,
            "missing_endpoints": [],
            "required_endpoints": {},
            "errors": [],
        }
        openapi: dict[str, Any] | None = None
        auth_failed = False

        try:
            openapi = await self.get_openapi()
            result["openapi_ok"] = True
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {401, 403}:
                auth_failed = True
                result["errors"].append(f"OpenAPI auth error: HTTP {status_code}")
            else:
                result["errors"].append(f"OpenAPI error: {exc}")

        if openapi is not None:
            for method, path in (
                REQUIRED_API_ENDPOINTS
                + CLIENT_ADD_ENDPOINTS
                + CLIENT_UPDATE_ENDPOINTS
                + INBOUND_CREATE_ENDPOINTS
                + INBOUND_UPDATE_ENDPOINTS
            ):
                exists = openapi_has_endpoint(openapi, method, path)
                key = f"{method} {path}"
                result["required_endpoints"][key] = exists
        else:
            for method, path in (
                REQUIRED_API_ENDPOINTS
                + CLIENT_ADD_ENDPOINTS
                + CLIENT_UPDATE_ENDPOINTS
                + INBOUND_CREATE_ENDPOINTS
                + INBOUND_UPDATE_ENDPOINTS
            ):
                result["required_endpoints"][f"{method} {path}"] = False

        read_missing = [
            f"{method} {path}"
            for method, path in REQUIRED_API_ENDPOINTS
            if not result["required_endpoints"].get(f"{method} {path}", False)
        ]
        add_ok = any(
            result["required_endpoints"].get(f"{method} {path}", False)
            for method, path in CLIENT_ADD_ENDPOINTS
        )
        update_ok = any(
            result["required_endpoints"].get(f"{method} {path}", False)
            for method, path in CLIENT_UPDATE_ENDPOINTS
        )
        result["client_add_ok"] = add_ok
        result["client_update_ok"] = update_ok
        inbound_create_ok = any(
            result["required_endpoints"].get(f"{method} {path}", False)
            for method, path in INBOUND_CREATE_ENDPOINTS
        )
        result["inbound_create_ok"] = inbound_create_ok
        result["inbound_create_endpoint"] = next(
            (
                path
                for method, path in INBOUND_CREATE_ENDPOINTS
                if result["required_endpoints"].get(f"{method} {path}", False)
            ),
            None,
        )
        result["client_add_endpoint"] = next(
            (path for method, path in CLIENT_ADD_ENDPOINTS if result["required_endpoints"].get(f"{method} {path}", False)),
            None,
        )
        result["client_update_endpoint"] = next(
            (path for method, path in CLIENT_UPDATE_ENDPOINTS if result["required_endpoints"].get(f"{method} {path}", False)),
            None,
        )
        result["missing_endpoints"] = read_missing
        if openapi is not None and not add_ok:
            result["missing_endpoints"].append("POST client add endpoint")
        if openapi is not None and not update_ok:
            result["missing_endpoints"].append("POST client update endpoint")
        if openapi is not None and not inbound_create_ok:
            result["missing_endpoints"].append("POST inbound create endpoint")
        inbound_update_ok = any(
            result["required_endpoints"].get(f"{method} {path}", False)
            for method, path in INBOUND_UPDATE_ENDPOINTS
        )
        result["inbound_update_ok"] = inbound_update_ok
        result["inbound_update_endpoint"] = next(
            (
                path
                for method, path in INBOUND_UPDATE_ENDPOINTS
                if result["required_endpoints"].get(f"{method} {path}", False)
            ),
            None,
        )
        if openapi is not None and not inbound_update_ok:
            result["missing_endpoints"].append("POST inbound update endpoint")

        try:
            inbounds = await self.list_inbounds()
            result["inbounds_ok"] = True
            result["auth_ok"] = True
            result["inbounds_count"] = len(inbounds)
            if openapi is None:
                result["required_endpoints"]["GET /panel/api/inbounds/list"] = True
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {401, 403}:
                auth_failed = True
                result["errors"].append(f"Inbounds API auth error: HTTP {status_code}")
            else:
                result["errors"].append(f"Inbounds API error: {exc}")

        if auth_failed:
            result["auth_ok"] = False
        elif result["openapi_ok"] or result["inbounds_ok"]:
            result["auth_ok"] = True

        result["ok"] = (
            bool(result["openapi_ok"])
            and bool(result["auth_ok"])
            and bool(result["inbounds_ok"])
            and bool(result.get("client_add_ok"))
            and bool(result.get("client_update_ok"))
            and bool(result.get("inbound_create_ok"))
            and bool(result.get("inbound_update_ok"))
            and not result["missing_endpoints"]
        )
        return result

    async def list_inbounds(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/panel/api/inbounds/list")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def get_inbound(self, inbound_id: int) -> dict[str, Any] | None:
        try:
            data = await self._request("GET", f"/panel/api/inbounds/get/{inbound_id}")
        except XUIError:
            return None
        if isinstance(data, dict) and data:
            return data
        return None

    async def get_inbound_by_remark_exact(self, remark: str) -> list[dict[str, Any]]:
        inbounds = await self.list_inbounds()
        matches = [
            inbound
            for inbound in inbounds
            if str(inbound.get("remark") or "") == remark
        ]
        result: list[dict[str, Any]] = []
        for inbound in matches:
            inbound_id = inbound.get("id")
            if inbound_id is None:
                continue
            full = await self.get_inbound(int(inbound_id))
            if full is not None:
                result.append(full)
        return result

    async def find_template_inbound(
        self,
        template_inbound_id: int | None = None,
        template_remark: str = "Moroz",
    ) -> dict[str, Any]:
        if template_inbound_id is not None:
            inbound = await self.get_inbound(template_inbound_id)
            if inbound is None:
                raise XUIError(f"template inbound {template_inbound_id} not found")
            return inbound
        matches = await self.get_inbound_by_remark_exact(template_remark)
        if not matches:
            raise XUIError(f"template inbound with remark {template_remark!r} not found")
        if len(matches) > 1:
            ids = ", ".join(str(item.get("id")) for item in matches)
            raise XUIError(f"multiple template inbounds with remark {template_remark!r}: {ids}")
        return matches[0]

    async def create_inbound_from_template_inbound(
        self,
        template_inbound: dict[str, Any],
        telegram_id: int,
        port: int,
    ) -> int:
        remark = f"tg_{telegram_id}"
        payload = build_inbound_payload_from_template(
            template_inbound,
            port=port,
            remark=remark,
            tag=f"tg_{telegram_id}",
        )
        payload["enable"] = True
        data = await self._request("POST", "/panel/api/inbounds/add", json=payload)
        if isinstance(data, dict) and data.get("id") is not None:
            return int(data["id"])

        after = await self.list_inbounds()
        candidates = [
            inbound
            for inbound in after
            if int(inbound.get("port") or 0) == port
            and str(inbound.get("remark") or "") == remark
        ]
        if len(candidates) == 1 and candidates[0].get("id") is not None:
            return int(candidates[0]["id"])
        raise XUIError("created inbound could not be identified after add")

    async def verify_created_inbound(
        self,
        inbound_id: int,
        template_inbound: dict[str, Any],
        telegram_id: int,
        port: int,
    ) -> dict[str, Any]:
        created = await self.get_inbound(inbound_id)
        if created is None:
            return {"ok": False, "errors": ["created inbound not found"], "inbound": None}
        errors: list[str] = []
        if str(created.get("remark") or "") != f"tg_{telegram_id}":
            errors.append("remark mismatch")
        if int(created.get("port") or 0) != port:
            errors.append("port mismatch")
        if created.get("protocol") != template_inbound.get("protocol"):
            errors.append("protocol mismatch")
        if extract_clients(created):
            errors.append("clients not empty")
        core_changes = compare_template_clone_core_fields(template_inbound, created)
        errors.extend(f"{field} mismatch" for field in core_changes)
        return {"ok": not errors, "errors": errors, "inbound": created}

    async def create_inbound_from_template(
        self,
        template_inbound_id: int,
        port: int,
        remark: str,
    ) -> dict[str, Any]:
        template = await self.get_inbound(template_inbound_id)
        if template is None:
            raise XUIError(f"template inbound {template_inbound_id} not found")
        before = await self.list_inbounds()
        before_ids = {int(inbound["id"]) for inbound in before if "id" in inbound}
        payload = build_inbound_payload_from_template(template, port=port, remark=remark)
        data = await self._request("POST", "/panel/api/inbounds/add", json=payload)

        if isinstance(data, dict) and data.get("id") is not None:
            created = await self.get_inbound(int(data["id"]))
            if created is not None:
                return created

        after = await self.list_inbounds()
        candidates = [
            inbound
            for inbound in after
            if int(inbound.get("id") or 0) not in before_ids
            and int(inbound.get("port") or 0) == port
            and str(inbound.get("remark") or "") == remark
        ]
        if len(candidates) == 1 and candidates[0].get("id") is not None:
            created = await self.get_inbound(int(candidates[0]["id"]))
            if created is not None:
                return created
        raise XUIError("created inbound could not be identified after add")

    async def update_inbound(self, inbound_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
        _method, path = await self._choose_endpoint(
            INBOUND_UPDATE_ENDPOINTS,
            ("POST", "/panel/api/inbounds/update/{id}"),
        )
        if path != "/panel/api/inbounds/update/{id}":
            raise XUIError("unsupported inbound update endpoint")
        await self._request("POST", f"/panel/api/inbounds/update/{inbound_id}", json=payload)
        return await self.get_inbound(inbound_id)

    async def count_active_clients(self, inbound_id: int) -> int:
        inbound = await self.get_inbound(inbound_id)
        if inbound is None:
            return 0
        return count_active_clients_in_inbound(inbound)

    def _default_flow(self, inbound: dict[str, Any]) -> str:
        for client in extract_clients(inbound):
            flow = str(client.get("flow", ""))
            if flow:
                return flow
        return ""

    async def add_client(
        self,
        inbound_id: int,
        telegram_id: int,
        title: str,
        client_uuid: str,
        email: str,
        expires_at: int | None,
        traffic_gb: int,
    ) -> None:
        inbound = await self.get_inbound(inbound_id)
        if inbound is None:
            raise XUIError(f"inbound {inbound_id} not found")

        telegram_id = int(telegram_id)
        if telegram_id <= 0:
            raise XUIError("telegram_id must be positive")

        total_gb = 0 if traffic_gb <= 0 else traffic_gb * 1024 * 1024 * 1024
        expiry_time = 0 if expires_at is None else expires_at * 1000
        client = {
            "id": client_uuid,
            "flow": self._default_flow(inbound),
            "email": email,
            "limitIp": 0,
            "totalGB": total_gb,
            "expiryTime": expiry_time,
            "enable": True,
            "tgId": telegram_id,
            "subId": email,
            "reset": 0,
            "comment": title,
        }
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [client]})}
        _method, path = await self._choose_endpoint(
            CLIENT_ADD_ENDPOINTS,
            ("POST", "/panel/api/inbounds/addClient"),
        )
        if path == "/panel/api/clients/add":
            await self._request(
                "POST",
                path,
                json={"client": client, "inboundIds": [inbound_id]},
            )
            return
        await self._request("POST", path, json=payload)

    async def disable_client(self, inbound_id: int, client_uuid: str) -> None:
        inbound = await self.get_inbound(inbound_id)
        if inbound is None:
            raise XUIError(f"inbound {inbound_id} not found")
        for client in extract_clients(inbound):
            if str(client.get("id")) != client_uuid:
                continue
            updated_client = dict(client)
            updated_client["enable"] = False
            if "enabled" in updated_client:
                updated_client["enabled"] = False
            _method, path = await self._choose_endpoint(
                CLIENT_UPDATE_ENDPOINTS,
                ("POST", "/panel/api/inbounds/updateClient/{uuid}"),
            )
            if path == "/panel/api/clients/update/{email}":
                email = str(updated_client.get("email") or "")
                if not email:
                    raise XUIError(f"client ...{client_uuid[-6:]} has no email for update")
                await self._request(
                    "POST",
                    f"/panel/api/clients/update/{quote(email, safe='')}",
                    json=updated_client,
                )
                return
            payload = {
                "id": inbound_id,
                "settings": json.dumps({"clients": [updated_client]}),
            }
            await self._request(
                "POST",
                f"/panel/api/inbounds/updateClient/{quote(client_uuid, safe='')}",
                json=payload,
            )
            return
        raise XUIError(f"client ...{client_uuid[-6:]} not found")

    async def disable_user_clients(self, inbound_id: int, telegram_id: int) -> None:
        inbound = await self.get_inbound(inbound_id)
        if inbound is None:
            raise XUIError(f"inbound {inbound_id} not found")
        client_ids = [
            str(client.get("id"))
            for client in extract_clients(inbound)
            if client.get("id") and client_belongs_to_tg(client, telegram_id) and client_is_enabled(client)
        ]
        for client_uuid in client_ids:
            await self.disable_client(inbound_id, client_uuid)
