from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Settings
from bot.xui_client import XUIService, extract_clients


def _missing(settings: Settings) -> list[str]:
    missing = []
    if not settings.xui_host:
        missing.append("XUI_HOST")
    if not settings.xui_token:
        missing.append("XUI_TOKEN")
    return missing


async def main() -> int:
    settings = Settings.from_env()
    missing = _missing(settings)
    if missing:
        print(f"SKIPPED: set {', '.join(missing)} in .env to run readonly XUI smoke.")
        return 0

    try:
        xui = XUIService(settings.xui_host, settings.xui_token)
    except RuntimeError as exc:
        if "httpx is required" in str(exc):
            print("ERROR: httpx is not installed for this Python interpreter.")
            print("Run: python3 -m pip install -r requirements.txt")
            print("Or use the project venv: .venv/bin/python scripts/smoke_real_xui_readonly.py")
            return 2
        raise
    try:
        report = await xui.api_check()
        print(f"api_check: {'ok' if report.get('ok') else 'failed'}")
        print(f"openapi_ok: {report.get('openapi_ok')}")
        print(f"auth_ok: {report.get('auth_ok')}")
        print(f"inbounds_ok: {report.get('inbounds_ok')}")
        print(
            "client_add_api: {status}{endpoint}".format(
                status="ok" if report.get("client_add_ok") else "failed",
                endpoint=f" ({report.get('client_add_endpoint')})" if report.get("client_add_endpoint") else "",
            )
        )
        print(
            "client_update_api: {status}{endpoint}".format(
                status="ok" if report.get("client_update_ok") else "failed",
                endpoint=f" ({report.get('client_update_endpoint')})" if report.get("client_update_endpoint") else "",
            )
        )
        required = report.get("required_endpoints", {})
        if required:
            print("required_endpoints:")
            for key in sorted(required):
                print(f"  {key}: {'OK' if required[key] else 'missing'}")
        if report.get("missing_endpoints"):
            print("missing_endpoints:")
            for endpoint in report["missing_endpoints"]:
                print(f"  {endpoint}")
        if report.get("errors"):
            for error in report["errors"]:
                print(f"error: {error}")
        try:
            openapi = await xui.get_openapi()
            info = openapi.get("info") if isinstance(openapi.get("info"), dict) else {}
            print(f"openapi: {openapi.get('openapi', 'unknown')}")
            print(f"openapi_title: {info.get('title', 'unknown')}")
            print(f"openapi_version: {info.get('version', 'unknown')}")
        except Exception as exc:
            print(f"openapi_version: unavailable ({exc})")
        if not report.get("inbounds_ok"):
            return 2
        inbounds = await xui.list_inbounds()
        print(f"inbounds: {len(inbounds)}")
        for inbound in inbounds:
            print(
                "id={id} remark={remark} protocol={protocol} port={port} client_count={clients}".format(
                    id=inbound.get("id"),
                    remark=inbound.get("remark", ""),
                    protocol=inbound.get("protocol"),
                    port=inbound.get("port"),
                    clients=len(extract_clients(inbound)),
                )
            )
    finally:
        await xui.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
