from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

_TEST_DIR = tempfile.TemporaryDirectory(prefix="tgvault-auto-reply-bot-")
os.environ["BOT_TOKEN"] = "123456:unit-test-token"
os.environ["TELEGRAM_API_ID"] = "12345"
os.environ["TELEGRAM_API_HASH"] = "unit-test-hash"
os.environ["ALLOWED_TELEGRAM_USER_IDS"] = "101"
os.environ["DATABASE_PATH"] = str(Path(_TEST_DIR.name) / "bot.sqlite3")
os.environ["SESSION_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")

from tg_manager import bot  # noqa: E402
from tg_manager.storage import Store  # noqa: E402


class FakeEvent:
    def __init__(self, *, peer_id: int, message_id: int, outgoing: bool = False, sender=None) -> None:
        self.is_private = True
        self.chat_id = peer_id
        self.message = SimpleNamespace(id=message_id, out=outgoing)
        self._sender = sender or SimpleNamespace(first_name="Ada", last_name="Lovelace", bot=False)

    async def get_sender(self):
        return self._sender


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, peer_id: int, text: str):
        self.sent.append((peer_id, text))
        return SimpleNamespace(id=900)


class AutoReplyBotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_store = bot.store
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(
            str(Path(self.tmp.name) / "test.sqlite3"),
            Fernet.generate_key().decode("ascii"),
        )
        self.account_id = self.store.add_account(101, "+15551234567", "", "session")
        bot.store = self.store
        bot.auto_reply_locks.clear()
        bot.auto_sending_peers.clear()

    def tearDown(self) -> None:
        bot.store = self.previous_store
        bot.auto_reply_locks.clear()
        bot.auto_sending_peers.clear()
        self.tmp.cleanup()

    async def test_no_auto_reply_is_sent_without_explicit_enablement(self) -> None:
        client = FakeClient()
        await bot.handle_auto_reply_event(FakeEvent(peer_id=7001, message_id=10), 101, self.account_id, client)
        self.assertEqual(client.sent, [])
        self.assertEqual(self.store.auto_reply_pending_count(101, self.account_id), 0)

    async def test_private_first_contact_receives_customized_auto_reply_and_marker(self) -> None:
        self.store.set_auto_reply_all(101, True, now=20_000)
        client = FakeClient()

        async def no_sleep(_seconds: float) -> None:
            return None

        with patch.object(bot.asyncio, "sleep", new=no_sleep):
            await bot.handle_auto_reply_event(FakeEvent(peer_id=7001, message_id=11), 101, self.account_id, client)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.sent[0][0], 7001)
        self.assertIn("Ada", client.sent[0][1])
        self.assertTrue(self.store.is_auto_reply_message(101, self.account_id, 7001, 900))
        self.assertEqual(self.store.list_auto_reply_messages(101, self.account_id)[0]["scene"], "welcome")

    async def test_human_outgoing_message_clears_manual_mode_and_pauses_account(self) -> None:
        self.store.set_auto_reply_all(101, True, now=20_000)
        client = FakeClient()
        await bot.handle_auto_reply_event(
            FakeEvent(peer_id=7001, message_id=12, outgoing=True), 101, self.account_id, client
        )
        state = self.store.get_auto_reply_settings(101, self.account_id)
        self.assertFalse(state["manual_auto"])
        self.assertGreater(state["paused_until"], state["last_human_at"])
        self.assertEqual(client.sent, [])

    async def test_group_or_bot_messages_are_ignored(self) -> None:
        self.store.set_auto_reply_all(101, True, now=20_000)
        client = FakeClient()
        group = FakeEvent(peer_id=-100123, message_id=13)
        group.is_private = False
        await bot.handle_auto_reply_event(group, 101, self.account_id, client)
        from_bot = FakeEvent(peer_id=7002, message_id=14, sender=SimpleNamespace(bot=True))
        await bot.handle_auto_reply_event(from_bot, 101, self.account_id, client)
        self.assertEqual(client.sent, [])
        self.assertEqual(self.store.auto_reply_pending_count(101, self.account_id), 0)


if __name__ == "__main__":
    unittest.main()
