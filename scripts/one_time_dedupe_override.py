"""Persistent, request-bound one-time exception to the direct URL dedupe guard."""
from __future__ import annotations

import json
import os
import time


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary_path = f"{path}.{os.getpid()}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temporary_path, path)


def claim_duplicate_override(
    path: str,
    *,
    canonical_url: str,
    request_key: str,
    chat_id: int,
    message_id: int,
    now: int | None = None,
) -> bool:
    """Claim an armed exception once for one fresh Telegram request.

    A claimed exception remains usable only by that same request key, allowing
    retryable pre-upload failures without granting a second request permission.
    """
    entries = _load(path)
    entry = entries.get(canonical_url)
    if not isinstance(entry, dict) or entry.get("canonical_url") != canonical_url:
        return False

    status = entry.get("status")
    if status == "claimed":
        return entry.get("request_key") == request_key
    if status != "armed":
        return False

    current = int(time.time() if now is None else now)
    try:
        expires_at = int(entry["expires_at"])
        allowed_chat_id = int(entry["chat_id"])
        after_message_id = int(entry["after_message_id"])
    except (KeyError, TypeError, ValueError):
        return False

    if current >= expires_at or int(chat_id) != allowed_chat_id or int(message_id) <= after_message_id:
        return False

    entry["status"] = "claimed"
    entry["request_key"] = request_key
    entry["claimed_at"] = current
    _save_atomic(path, entries)
    return True


def consume_duplicate_override(
    path: str,
    *,
    canonical_url: str,
    request_key: str,
    now: int | None = None,
) -> bool:
    """Permanently consume a claimed exception after a confirmed upload."""
    entries = _load(path)
    entry = entries.get(canonical_url)
    if (
        not isinstance(entry, dict)
        or entry.get("canonical_url") != canonical_url
        or entry.get("status") != "claimed"
        or entry.get("request_key") != request_key
    ):
        return False
    entry["status"] = "consumed"
    entry["consumed_at"] = int(time.time() if now is None else now)
    _save_atomic(path, entries)
    return True
