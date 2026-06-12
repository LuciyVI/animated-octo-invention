from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Settings
from bot.utils.security import mask_uuid
from bot.xui_client import (
    XUIService,
    client_is_enabled,
    compare_inbound_immutable_fields,
    extract_clients,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Modify a real 3x-ui inbound by adding and disabling a smoke client.")
    parser.add_argument("inbound_id", type=int)
    parser.add_argument(
        "--i-understand-this-will-modify-3x-ui",
        action="store_true",
        help="Required explicit acknowledgement.",
    )
    return parser.parse_args()


def _find_client(inbound: dict, client_uuid: str) -> dict | None:
    for client in extract_clients(inbound):
        if str(client.get("id")) == client_uuid:
            return client
    return None


async def main() -> int:
    args = parse_args()
    if not args.i_understand_this_will_modify_3x_ui:
        print("ERROR: this script modifies 3x-ui. Re-run with --i-understand-this-will-modify-3x-ui.")
        return 2

    settings = Settings.from_env()
    if not settings.xui_host or not settings.xui_token:
        print("ERROR: set XUI_HOST and XUI_TOKEN in .env.")
        return 1

    try:
        xui = XUIService(settings.xui_host, settings.xui_token)
    except RuntimeError as exc:
        if "httpx is required" in str(exc):
            print("ERROR: httpx is not installed for this Python interpreter.")
            print("Run: python3 -m pip install -r requirements.txt")
            print("Or use the project venv: .venv/bin/python scripts/smoke_real_xui_add_client.py <inbound_id> --i-understand-this-will-modify-3x-ui")
            return 2
        raise
    client_uuid = str(uuid.uuid4())
    email = f"tg_smoke_test_{int(time.time())}"
    try:
        before = await xui.get_inbound(args.inbound_id)
        if before is None:
            print(f"ERROR: inbound {args.inbound_id} not found.")
            return 1

        await xui.add_client(
            inbound_id=args.inbound_id,
            telegram_id=0,
            title="smoke-test",
            client_uuid=client_uuid,
            email=email,
            expires_at=None,
            traffic_gb=0,
        )
        after_add = await xui.get_inbound(args.inbound_id)
        if after_add is None or _find_client(after_add, client_uuid) is None:
            print(f"ERROR: smoke client {mask_uuid(client_uuid)} was not found after add.")
            return 1
        changed_after_add = compare_inbound_immutable_fields(before, after_add)
        if changed_after_add:
            print(f"ERROR: immutable inbound fields changed after add: {', '.join(changed_after_add)}")
            return 1

        await xui.disable_client(args.inbound_id, client_uuid)
        after_disable = await xui.get_inbound(args.inbound_id)
        if after_disable is None:
            print("ERROR: inbound disappeared after disable.")
            return 1
        client = _find_client(after_disable, client_uuid)
        if client is None or client_is_enabled(client):
            print(f"ERROR: smoke client {mask_uuid(client_uuid)} was not disabled.")
            return 1

        changed = compare_inbound_immutable_fields(before, after_disable)
        if changed:
            print(f"ERROR: immutable inbound fields changed: {', '.join(changed)}")
            return 1

        print(f"OK: added and disabled smoke client {mask_uuid(client_uuid)} ({email}); immutable fields unchanged.")
        return 0
    finally:
        await xui.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
