from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.access_control import AccessControlService
from bot.auto_provision import AutoProvisionService
from bot.config import Settings
from bot.db import Database
from bot.xui_client import XUIService, extract_clients


class DummyBot:
    async def get_chat_member(self, chat_id: int, user_id: int) -> object:
        return SimpleNamespace(status="member", is_member=None)


async def main() -> int:
    settings = Settings.from_env()
    if not settings.xui_host or not settings.xui_token:
        print("SKIPPED: XUI_HOST and XUI_TOKEN are required")
        return 0
    db = Database(settings.db_path)
    db.init()
    xui = XUIService(settings.xui_host, settings.xui_token)
    access = AccessControlService(DummyBot(), settings)  # type: ignore[arg-type]
    auto = AutoProvisionService(db, xui, settings, access)
    try:
        api = await xui.api_check()
        print(f"api_check: {'ok' if api.get('ok') else 'failed'}")
        template = await auto.check_template()
        print(f"template_remark: {settings.template_inbound_remark}")
        print(f"template_found: {'yes' if template.found else 'no'}")
        print(f"template_ok: {'yes' if template.ok else 'no'}")
        print(f"reason: {template.reason}")
        if not template.inbound:
            return 1
        inbound = template.inbound
        print(f"template_id: {inbound.get('id')}")
        print(f"template_protocol: {inbound.get('protocol')}")
        print(f"template_port: {inbound.get('port')}")
        print(f"template_clients: {len(extract_clients(inbound))} (will not be copied)")
        print(f"link_builder: {'yes' if template.link_supported else 'no'}")
        port = await auto.planned_free_port()
        print("planned inbound:")
        print("  remark: tg_<telegram_id>")
        print(f"  port: {port}")
        print(f"  range: {settings.port_min}-{settings.port_max}")
        print("mode: dry-run, no 3x-ui changes")
        return 0 if template.ok else 1
    finally:
        await xui.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
