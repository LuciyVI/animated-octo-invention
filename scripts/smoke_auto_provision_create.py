from __future__ import annotations

import argparse
import asyncio
import time
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.access_control import AccessControlService
from bot.auto_provision import AutoProvisionService
from bot.config import Settings
from bot.db import Database
from bot.xui_client import XUIService, compare_inbound_immutable_fields, extract_clients


class DummyBot:
    async def get_chat_member(self, chat_id: int, user_id: int) -> object:
        return SimpleNamespace(status="member", is_member=None)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Guarded real 3x-ui auto-provision smoke test")
    parser.add_argument("--telegram-id", type=int, default=900000000000)
    parser.add_argument("--i-understand-this-will-create-inbound", action="store_true")
    args = parser.parse_args()
    if not args.i_understand_this_will_create_inbound:
        print("ERROR: this script creates a real inbound in 3x-ui.")
        print("Re-run with --i-understand-this-will-create-inbound to proceed.")
        return 2

    settings = Settings.from_env()
    db = Database(settings.db_path)
    db.init()
    xui = XUIService(settings.xui_host, settings.xui_token)
    access = AccessControlService(DummyBot(), settings)  # type: ignore[arg-type]
    auto = AutoProvisionService(db, xui, settings, access)
    try:
        template = await auto.find_template_inbound()
        template_before = await xui.get_inbound(int(template["id"]))
        if template_before is None:
            print("ERROR: template disappeared")
            return 1
        port = await auto.planned_free_port()
        tg_id = int(args.telegram_id + int(time.time()) % 100000)
        inbound_id = await xui.create_inbound_from_template_inbound(template_before, tg_id, port)
        verify = await xui.verify_created_inbound(inbound_id, template_before, tg_id, port)
        template_after = await xui.get_inbound(int(template_before["id"]))
        template_changes = [] if template_after is None else compare_inbound_immutable_fields(template_before, template_after)
        created = await xui.get_inbound(inbound_id)
        print(f"created_inbound_id: {inbound_id}")
        print(f"created_port: {port}")
        print(f"verify_ok: {'yes' if verify.get('ok') else 'no'}")
        print(f"verify_errors: {', '.join(verify.get('errors') or []) or 'none'}")
        print(f"template_changed: {'yes' if template_changes else 'no'}")
        print(f"created_clients: {len(extract_clients(created or {}))}")
        print("NOTE: inbound was not deleted automatically. Remove it manually if this was only a test.")
        return 0 if verify.get("ok") and not template_changes else 1
    finally:
        await xui.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
