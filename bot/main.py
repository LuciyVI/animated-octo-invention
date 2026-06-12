from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress

from aiogram import Bot, Dispatcher

from bot.access_control import AccessControlService
from bot.auto_provision import AutoProvisionService
from bot.config import Settings
from bot.db import Database
from bot.handlers import admin, user
from bot.middlewares.access import GroupAccessMiddleware
from bot.services import BotService
from bot.xui_client import XUIService


async def expiration_worker(service: BotService, interval_seconds: int) -> None:
    while True:
        try:
            expired_count = await service.expire_users()
            if expired_count:
                logging.info("Expired %s users", expired_count)
        except Exception:
            logging.exception("Expiration worker failed")
        await asyncio.sleep(interval_seconds)


async def async_main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    settings.validate_runtime()

    db = Database(settings.db_path)
    db.init()
    xui = XUIService(settings.xui_host, settings.xui_token)
    service = BotService(db, xui, settings)

    bot = Bot(settings.bot_token)
    access_control = AccessControlService(bot, settings)
    auto_provision = AutoProvisionService(db, xui, settings, access_control)
    dp = Dispatcher()
    dp["settings"] = settings
    dp["db"] = db
    dp["xui"] = xui
    dp["service"] = service
    dp["access_control"] = access_control
    dp["auto_provision"] = auto_provision
    access_middleware = GroupAccessMiddleware()
    dp.message.middleware(access_middleware)
    dp.callback_query.middleware(access_middleware)
    dp.include_router(user.router)
    dp.include_router(admin.router)

    worker = asyncio.create_task(expiration_worker(service, settings.expiration_check_seconds))
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        await xui.close()
        await bot.session.close()
        db.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
