from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from bot.xui_client import extract_clients, extract_stream_settings


class LinkBuildError(ValueError):
    pass


def _first_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                return item
    return ""


def _find_client(inbound: dict[str, Any], client_uuid: str, email: str | None = None) -> dict[str, Any]:
    for client in extract_clients(inbound):
        if str(client.get("id")) == client_uuid:
            return client
        if email is not None and str(client.get("email")) == email:
            return client
    return {}


def is_link_supported(inbound: dict[str, Any]) -> bool:
    supported, _reason = explain_link_support(inbound)
    return supported


def explain_link_support(inbound: dict[str, Any]) -> tuple[bool, str]:
    protocol = str(inbound.get("protocol", "")).lower()
    if protocol != "vless":
        return False, f"protocol '{protocol or 'unknown'}' is not supported"
    stream = extract_stream_settings(inbound)
    security = str(stream.get("security", "")).lower()
    network = str(stream.get("network", "tcp")).lower()
    if security == "reality":
        return True, "VLESS REALITY is supported"
    if security == "tls" and network == "ws":
        return True, "VLESS TLS/WS is supported"
    return False, f"VLESS network='{network}' security='{security}' is not supported"


def build_share_link(
    inbound: dict[str, Any],
    client_uuid: str,
    email: str,
    public_host: str,
    name: str | None = None,
) -> str:
    protocol = str(inbound.get("protocol", "")).lower()
    if protocol != "vless":
        raise LinkBuildError(f"link generation for protocol '{protocol or 'unknown'}' is not supported")

    port = inbound.get("port")
    if port is None:
        raise LinkBuildError("inbound port is missing")

    stream = extract_stream_settings(inbound)
    network = str(stream.get("network", "tcp")).lower()
    security = str(stream.get("security", "")).lower()
    display_name = name or email
    client = _find_client(inbound, client_uuid, email)
    flow = str(client.get("flow", ""))

    if security == "reality":
        return _build_vless_reality(
            client_uuid=client_uuid,
            public_host=public_host,
            port=port,
            network=network or "tcp",
            stream=stream,
            flow=flow,
            name=display_name,
        )
    if security == "tls" and network == "ws":
        return _build_vless_tls_ws(
            client_uuid=client_uuid,
            public_host=public_host,
            port=port,
            stream=stream,
            name=display_name,
        )

    raise LinkBuildError(
        f"link generation for VLESS network='{network}' security='{security}' is not supported"
    )


def _build_vless_reality(
    client_uuid: str,
    public_host: str,
    port: int | str,
    network: str,
    stream: dict[str, Any],
    flow: str,
    name: str,
) -> str:
    reality = stream.get("realitySettings") or {}
    if not isinstance(reality, dict):
        reality = {}
    nested_settings = reality.get("settings") or {}
    if not isinstance(nested_settings, dict):
        nested_settings = {}

    public_key = str(nested_settings.get("publicKey") or reality.get("publicKey") or "")
    sni = (
        str(nested_settings.get("serverName") or "")
        or _first_string(reality.get("serverNames"))
        or _first_string(reality.get("serverName"))
    )
    short_id = (
        str(nested_settings.get("shortId") or "")
        or _first_string(reality.get("shortIds"))
        or _first_string(reality.get("shortId"))
    )
    fingerprint = str(nested_settings.get("fingerprint") or reality.get("fingerprint") or "chrome")

    if not public_key:
        raise LinkBuildError("REALITY public key is missing in streamSettings")

    params: dict[str, str] = {
        "type": network or "tcp",
        "security": "reality",
        "pbk": public_key,
        "fp": fingerprint,
    }
    if sni:
        params["sni"] = sni
    if short_id:
        params["sid"] = short_id
    if flow:
        params["flow"] = flow

    return f"vless://{client_uuid}@{public_host}:{port}?{urlencode(params)}#{quote(name)}"


def _build_vless_tls_ws(
    client_uuid: str,
    public_host: str,
    port: int | str,
    stream: dict[str, Any],
    name: str,
) -> str:
    tls_settings = stream.get("tlsSettings") or {}
    ws_settings = stream.get("wsSettings") or {}
    if not isinstance(tls_settings, dict):
        tls_settings = {}
    if not isinstance(ws_settings, dict):
        ws_settings = {}

    headers = ws_settings.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}

    path = str(ws_settings.get("path") or "/")
    sni = str(tls_settings.get("serverName") or headers.get("Host") or "")
    host = str(headers.get("Host") or sni or public_host)

    params = {
        "type": "ws",
        "security": "tls",
        "host": host,
        "path": path,
    }
    if sni:
        params["sni"] = sni

    return f"vless://{client_uuid}@{public_host}:{port}?{urlencode(params)}#{quote(name)}"
