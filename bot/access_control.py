from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from aiogram import Bot

try:
    from aiogram.exceptions import TelegramAPIError
except ImportError:  # pragma: no cover
    TelegramAPIError = Exception  # type: ignore[misc,assignment]

from bot.config import Settings
from bot.utils.security import is_admin_id


ALLOWED_MEMBER_STATUSES = {"creator", "administrator", "member"}
DENIED_MEMBER_STATUSES = {"left", "kicked"}


@dataclass(frozen=True)
class MembershipCacheEntry:
    result: dict[str, Any]
    checked_at: float


class AccessControlService:
    def __init__(self, bot: Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings
        self._cache: dict[int, MembershipCacheEntry] = {}

    async def is_allowed_user(self, user_id: int) -> bool:
        status = await self.get_membership_status(user_id)
        return bool(status.get("allowed"))

    async def get_membership_status(self, user_id: int) -> dict[str, Any]:
        if not self.settings.require_group_membership:
            return {
                "enabled": False,
                "allowed": True,
                "status": "disabled",
                "is_member": None,
                "cache_hit": False,
                "checked_at": int(time.time()),
                "error": None,
                "admin_bypass": False,
                "group_id": self.settings.access_group_id,
                "user_id": user_id,
            }

        if is_admin_id(user_id, self.settings.admin_ids):
            return {
                "enabled": True,
                "allowed": True,
                "status": "admin_bypass",
                "is_member": True,
                "cache_hit": False,
                "checked_at": int(time.time()),
                "error": None,
                "admin_bypass": True,
                "group_id": self.settings.access_group_id,
                "user_id": user_id,
            }

        now = time.time()
        cached = self._cache.get(user_id)
        if cached and now - cached.checked_at <= self.settings.group_membership_cache_ttl_sec:
            result = dict(cached.result)
            result["cache_hit"] = True
            return result

        result = await self._fetch_membership_status(user_id)
        self._cache[user_id] = MembershipCacheEntry(result=dict(result), checked_at=now)
        return result

    async def invalidate_user_cache(self, user_id: int) -> None:
        self._cache.pop(user_id, None)

    async def _fetch_membership_status(self, user_id: int) -> dict[str, Any]:
        checked_at = int(time.time())
        group_id = self.settings.access_group_id
        if group_id is None:
            return {
                "enabled": True,
                "allowed": False,
                "status": "config_error",
                "is_member": None,
                "cache_hit": False,
                "checked_at": checked_at,
                "error": "ACCESS_GROUP_ID is not configured",
                "admin_bypass": False,
                "group_id": group_id,
                "user_id": user_id,
            }

        try:
            member = await self.bot.get_chat_member(chat_id=group_id, user_id=user_id)
        except TelegramAPIError as exc:
            logging.warning("Telegram group membership check failed for user_id=%s: %s", user_id, exc)
            return {
                "enabled": True,
                "allowed": False,
                "status": "telegram_api_error",
                "is_member": None,
                "cache_hit": False,
                "checked_at": checked_at,
                "error": str(exc),
                "admin_bypass": False,
                "group_id": group_id,
                "user_id": user_id,
            }
        except Exception as exc:
            logging.warning("Group membership check failed for user_id=%s: %s", user_id, exc)
            return {
                "enabled": True,
                "allowed": False,
                "status": "membership_error",
                "is_member": None,
                "cache_hit": False,
                "checked_at": checked_at,
                "error": str(exc),
                "admin_bypass": False,
                "group_id": group_id,
                "user_id": user_id,
            }

        raw_status = _status_to_str(getattr(member, "status", "unknown"))
        is_member = getattr(member, "is_member", None)
        allowed = raw_status in ALLOWED_MEMBER_STATUSES or (
            raw_status == "restricted" and is_member is True
        )
        if raw_status in DENIED_MEMBER_STATUSES:
            allowed = False
        return {
            "enabled": True,
            "allowed": allowed,
            "status": raw_status,
            "is_member": is_member,
            "cache_hit": False,
            "checked_at": checked_at,
            "error": None,
            "admin_bypass": False,
            "group_id": group_id,
            "user_id": user_id,
        }


def _status_to_str(status: object) -> str:
    value = getattr(status, "value", status)
    return str(value).lower()
