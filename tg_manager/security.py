from __future__ import annotations


class AllowlistConfigurationError(ValueError):
    """Raised when the bot does not have a valid, non-empty user allowlist."""


def parse_allowed_user_ids(raw: str | None) -> frozenset[int]:
    """Parse a mandatory comma-separated allowlist; empty or malformed input fails closed."""
    if raw is None or not raw.strip():
        raise AllowlistConfigurationError(
            "ALLOWED_TELEGRAM_USER_IDS must list at least one Telegram user ID"
        )

    parsed: set[int] = set()
    for item in raw.split(","):
        value = item.strip()
        if not value or not value.isascii() or not value.isdecimal():
            raise AllowlistConfigurationError(
                "ALLOWED_TELEGRAM_USER_IDS must be comma-separated positive integers"
            )
        user_id = int(value)
        if user_id <= 0:
            raise AllowlistConfigurationError(
                "ALLOWED_TELEGRAM_USER_IDS must contain only positive integers"
            )
        parsed.add(user_id)

    if not parsed:
        raise AllowlistConfigurationError(
            "ALLOWED_TELEGRAM_USER_IDS must list at least one Telegram user ID"
        )
    return frozenset(parsed)


def is_allowed_user_id(user_id: int | None, allowed_user_ids: frozenset[int]) -> bool:
    """Return false for missing identities and IDs not explicitly listed."""
    return user_id is not None and user_id in allowed_user_ids
