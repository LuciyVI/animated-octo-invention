from __future__ import annotations

from typing import Iterable


def is_admin_id(tg_id: int | None, admin_ids: Iterable[int]) -> bool:
    return tg_id is not None and int(tg_id) in set(admin_ids)


def mask_uuid(value: str) -> str:
    if len(value) <= 6:
        return "..." + value
    return "..." + value[-6:]


def mask_secret(value: object, visible: int = 4) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= visible * 2:
        return "***"
    return f"{text[:visible]}...{text[-visible:]}"


def is_private_chat_type(chat_type: str) -> bool:
    return chat_type == "private"
