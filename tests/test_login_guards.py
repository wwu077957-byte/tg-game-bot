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
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: object) -> None:
        self.replies.append(text)


class HandlerRecorder:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def add_handler(self, handler: object) -> None:
        self.handlers.append(handler)


class LoginFlowGuardTests(unittest.IsolatedAsyncioTestCase):
    user_id = 101

    def setUp(self) -> None:
        bot.flows.pop(self.user_id, None)
        gate = patch.object(bot, "require_private", new=AsyncMock(return_value=True))
        gate.start()
        self.addCleanup(gate.stop)

    def tearDown(self) -> None:
        bot.flows.pop(self.user_id, None)

    def update(self, message: FakeMessage | None = None):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=self.user_id),
            effective_chat=SimpleNamespace(type="private"),
            effective_message=message or FakeMessage(),
        )

    async def test_qr_command_starts_with_explicit_consent(self) -> None:
        message = FakeMessage()
        await bot.qr_start(self.update(message), SimpleNamespace(bot=object()))

        self.assertEqual(bot.flows[self.user_id].state, "consent")
        self.assertTrue(any("/consent" in text for text in message.replies))
        self.assertTrue(any("二维码" in text or "QR" in text for text in message.replies))

    async def test_consent_continues_only_the_pending_qr_flow(self) -> None:
        bot.flows[self.user_id] = bot.Flow(state="consent")
        mock_qr_start = AsyncMock()
        qr_start = patch.object(bot, "qr_start", new=mock_qr_start)
        qr_start.start()
        self.addCleanup(qr_start.stop)

        await bot.consent_login(self.update(), SimpleNamespace(bot=object()))

        self.assertEqual(bot.flows[self.user_id].state, "qr_authorized")
        mock_qr_start.assert_awaited_once()

    async def test_consent_without_a_pending_flow_points_to_qr(self) -> None:
        message = FakeMessage()
        await bot.consent_login(self.update(message), SimpleNamespace(bot=object()))

        self.assertTrue(any("/qr" in text for text in message.replies))
        self.assertTrue(all("/add" not in text for text in message.replies))

    async def test_registered_routes_have_no_phone_login_or_code_commands(self) -> None:
        app = HandlerRecorder()
        bot.register_handlers(app)  # type: ignore[arg-type]

        commands: set[str] = set()
        callback_patterns: list[str] = []
        for handler in app.handlers:
            if isinstance(handler, bot.CommandHandler):
                commands.update(handler.commands)
            elif isinstance(handler, bot.CallbackQueryHandler):
                pattern = handler.pattern
                callback_patterns.append(getattr(pattern, "pattern", str(pattern)))

        self.assertIn("qr", commands)
        self.assertIn("consent", commands)
        self.assertTrue({"add", "code", "resend"}.isdisjoint(commands))
        self.assertFalse(any("ctry:" in pattern for pattern in callback_patterns))

    async def test_qr_password_flow_expiry_clears_old_state(self) -> None:
        message = FakeMessage("a message")
        bot.flows[self.user_id] = bot.Flow(
            state="qr_password",
            started_at=time.monotonic() - bot.FLOW_TTL_SECONDS - 1,
        )

        await bot.handle_text(self.update(message), SimpleNamespace(bot=object()))

        self.assertNotIn(self.user_id, bot.flows)
        self.assertTrue(any("/qr" in text for text in message.replies))

    async def test_menus_expose_qr_but_no_phone_or_code_login(self) -> None:
        for language, forbidden in (
            ("zh", {"添加账号", "获取验证码"}),
            ("en", {"Add account", "Get code"}),
        ):
            with self.subTest(language=language):
                markup = bot.menu_for(language)
                labels = {button.text for row in markup.keyboard for button in row}
                self.assertIn("🔳 QR 登录" if language == "zh" else "🔳 QR login", labels)
                self.assertTrue(forbidden.isdisjoint(labels))


if __name__ == "__main__":
    unittest.main()
