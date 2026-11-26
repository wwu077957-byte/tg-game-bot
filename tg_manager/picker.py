from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

ACCOUNT_PICKER_ACTIONS = frozenset({"code", "devices", "twofa"})
MAX_ACCOUNT_ID = 2**63 - 1
_CALLBACK_RE = re.compile(r"^acctpick:(code|devices|twofa):([1-9][0-9]{0,18})$")


def make_picker_callback(action: str, account_id: int) -> str:
    if action not in ACCOUNT_PICKER_ACTIONS:
        raise ValueError("unsupported account picker action")
    if type(account_id) is not int or not 1 <= account_id <= MAX_ACCOUNT_ID:
        raise ValueError("invalid account id")
    return f"acctpick:{action}:{account_id}"


def parse_picker_callback(data: str | None) -> tuple[str, int] | None:
    if not isinstance(data, str):
        return None
    match = _CALLBACK_RE.fullmatch(data)
    if not match:
        return None
    account_id = int(match.group(2))
    if account_id > MAX_ACCOUNT_ID:
        return None
    return match.group(1), account_id


def build_account_picker(
    rows: Sequence[dict[str, Any]],
    action: str,
    label_for: Callable[[dict[str, Any]], str],
) -> InlineKeyboardMarkup:
    if action not in ACCOUNT_PICKER_ACTIONS:
        raise ValueError("unsupported account picker action")
    keyboard = [
        [InlineKeyboardButton(label_for(row), callback_data=make_picker_callback(action, row["id"]))]
        for row in rows
    ]
    return InlineKeyboardMarkup(keyboard)
