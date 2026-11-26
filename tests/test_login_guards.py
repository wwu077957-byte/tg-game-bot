from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet


_TEST_DIR = tempfile.TemporaryDirectory(prefix="tgvault-login-guard-tests-")
os.environ["BOT_TOKEN"] = "123456:unit-test-token"
os.environ["TELEGRAM_API_ID"] = "12345"
os.environ["TELEGRAM_API_HASH"] = "unit-test-hash"
os.environ["ALLOWED_TELEGRAM_USER_IDS"] = "101"
os.environ["DATABASE_PATH"] = str(Path(_TEST_DIR.name) / "guard-tests.sqlite3")
os.environ["SESSION_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")

from tg_manager import bot  # noqa: E402


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: object) -> None:
        self.replies.append(text)


class FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def answer(self, *args: object, **kwargs: object) -> None:
        self.answers.append((args, kwargs))


class LoginFlowExpiryGuardTests(unittest.IsolatedAsyncioTestCase):
    user_id = 101

    def setUp(self) -> None:
        bot.flows.pop(self.user_id, None)
        gate = patch.object(bot, "require_private", new=AsyncMock(return_value=True))
        gate.start()
        self.addCleanup(gate.stop)

    def tearDown(self) -> None:
        bot.flows.pop(self.user_id, None)

    def expired_code_flow(self) -> bot.Flow:
        return bot.Flow(
            state="code",
            phone="+14155552671",
            phone_code_hash="expired-hash",
            started_at=time.monotonic() - bot.FLOW_TTL_SECONDS - 1,
        )

    def update(self, message: FakeMessage | None = None, query: FakeCallbackQuery | None = None):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=self.user_id),
            effective_chat=SimpleNamespace(type="private"),
            effective_message=message or FakeMessage(),
            callback_query=query,
        )

    async def test_resend_rejects_expired_login_hash(self) -> None:
        message = FakeMessage()
        bot.flows[self.user_id] = self.expired_code_flow()

        await bot.resend_code(self.update(message), SimpleNamespace(bot=object()))

        self.assertNotIn(self.user_id, bot.flows)
        self.assertTrue(any("已过期" in text for text in message.replies))

    async def test_login_code_lookup_rejects_expired_flow(self) -> None:
        message = FakeMessage()
        bot.flows[self.user_id] = self.expired_code_flow()

        await bot.retrieve_login_code(self.user_id, 1, message, None)

        self.assertNotIn(self.user_id, bot.flows)
        self.assertTrue(any("已过期" in text for text in message.replies))

    async def test_country_picker_rejects_expired_button(self) -> None:
        query = FakeCallbackQuery("ctry:select:US")
        bot.flows[self.user_id] = bot.Flow(
            state="country",
            country_matches=["US"],
            started_at=time.monotonic() - bot.FLOW_TTL_SECONDS - 1,
        )

        await bot.country_picker_callback(self.update(query=query), SimpleNamespace(bot=object()))

        self.assertNotIn(self.user_id, bot.flows)
        self.assertTrue(query.answers)
        self.assertIn("已过期", str(query.answers[0][0][0]))

    async def test_code_picker_rejects_expired_flow(self) -> None:
        message = FakeMessage()
        bot.flows[self.user_id] = self.expired_code_flow()

        await bot.send_account_picker(self.update(message), "code")

        self.assertNotIn(self.user_id, bot.flows)
        self.assertTrue(any("已过期" in text for text in message.replies))


if __name__ == "__main__":
    unittest.main()
