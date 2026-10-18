"""Fail-closed configuration for Telegram webhook access."""

import re


def validate_webhook_secret(value: str | None) -> str:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise RuntimeError(
            "TELEGRAM_WEBHOOK_SECRET must contain 1-256 letters, digits, _ or -"
        )
    return value


def parse_allowed_user_ids(value: str | None) -> frozenset[int]:
    parts = value.split(",") if value else []
    if not parts or any(not re.fullmatch(r"[0-9]+", p.strip()) for p in parts):
        raise RuntimeError(
            "TELEGRAM_ALLOWED_USER_IDS must be comma-separated positive numeric user IDs"
        )
    ids = frozenset(int(p.strip()) for p in parts)
    if any(user_id <= 0 or user_id > 2**52 - 1 for user_id in ids):
        raise RuntimeError("TELEGRAM_ALLOWED_USER_IDS contains an invalid user ID")
    return ids
