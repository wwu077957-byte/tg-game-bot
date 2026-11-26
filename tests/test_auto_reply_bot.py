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


class FakeAccountReader:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def __call__(self, _request: object):
        return SimpleNamespace(authorizations=[], has_password=True)


class FakeGateMessage:
    def __init__(self) -> None:
        self.replies: list[tuple[str, dict[str, object]]] = []

    async def reply_text(self, text: str, **kwargs: object) -> None:
        self.replies.append((text, dict(kwargs)))


class FakeGateBot:
    def __init__(self, status: str = "member") -> None:
        self.status = status
        self.calls: list[tuple[str, int]] = []

    async def get_chat_member(self, channel_id: str, user_id: int):
        self.calls.append((channel_id, user_id))
        return SimpleNamespace(status=self.status)


class FakeGateQuery:
    def __init__(self, data: str, message: FakeGateMessage) -> None:
        self.data = data
        self.message = message
        self.answers: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def answer(self, *args: object, **kwargs: object) -> None:
        self.answers.append((args, dict(kwargs)))


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
        bot.pending_human_challenges.clear()

    def tearDown(self) -> None:
        bot.store = self.previous_store
        bot.auto_reply_locks.clear()
        bot.auto_sending_peers.clear()
        bot.pending_human_challenges.clear()
        self.tmp.cleanup()

    def test_account_labels_follow_language(self) -> None:
        row = self.store.get_account(101, self.account_id)
        row.update({"label": "Work", "group_name": "Friends"})
        self.assertIn("Group: Friends", bot.format_account(row, "en"))
        self.assertIn("分组：Friends", bot.format_account(row, "zh"))

    def test_auto_reply_scene_labels_use_language_without_changing_templates(self) -> None:
        english = [button.text for row in bot.auto_scene_keyboard(1, "en").inline_keyboard for button in row]
        chinese = [button.text for row in bot.auto_scene_keyboard(1, "zh").inline_keyboard for button in row]
        self.assertIn("First contact/welcome", english)
        self.assertIn("首次接触/欢迎语", chinese)
        self.assertEqual(bot.scene_labels("en")["welcome"], "First contact/welcome")
        self.assertIn("你好", bot.store.get_auto_reply_settings(101, self.account_id)["scenes"]["welcome"])

    async def test_device_and_twofa_success_screens_use_user_id_and_english(self) -> None:
        self.store.set_language(101, "en")
        message = FakeGateMessage()
        with patch.object(bot, "TelegramClient", FakeAccountReader), patch.object(bot, "StringSession", lambda _session: "session"):
            await bot.show_devices(101, self.account_id, message)
            await bot.show_twofa(101, self.account_id, message)
        self.assertIn("Telegram login devices", message.replies[0][0])
        self.assertIn("Two-step verification for this account: Enabled", message.replies[1][0])
        self.assertIsNotNone(message.replies[0][1]["reply_markup"])
        self.assertIsNotNone(message.replies[1][1]["reply_markup"])

    async def test_rename_flow_success_uses_saved_language(self) -> None:
        self.store.set_language(101, "en")
        self.store.mark_human_verified(101)
        bot.flows[101] = bot.Flow(state="rename")
        message = FakeGateMessage()
        message.text = f"{self.account_id} Work"
        update = SimpleNamespace(effective_user=SimpleNamespace(id=101), effective_chat=SimpleNamespace(type="private"), effective_message=message)
        await bot.handle_text(update, SimpleNamespace(bot=FakeGateBot()))
        self.assertEqual(message.replies[0][0], "Label updated.")
        self.assertEqual(self.store.get_account(101, self.account_id)["label"], "Work")

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

    async def test_simple_custom_reply_is_used_for_any_auto_reply_scene(self) -> None:
        self.store.set_auto_reply_all(101, True, now=20_000)
        config = self.store.get_auto_reply_settings(101, self.account_id)
        config["simple_reply"] = "Hi {name}, I will get back to you soon."
        self.store.save_auto_reply_config(101, self.account_id, config)
        client = FakeClient()

        async def no_sleep(_seconds: float) -> None:
            return None

        with patch.object(bot.asyncio, "sleep", new=no_sleep):
            await bot.handle_auto_reply_event(FakeEvent(peer_id=7003, message_id=15), 101, self.account_id, client)
        self.assertEqual(client.sent, [(7003, "Hi Ada Lovelace, I will get back to you soon.")])

    def test_channel_membership_status_requires_actual_membership(self) -> None:
        self.assertTrue(bot.is_channel_member(SimpleNamespace(status="member")))
        self.assertTrue(bot.is_channel_member(SimpleNamespace(status="restricted", is_member=True)))
        self.assertFalse(bot.is_channel_member(SimpleNamespace(status="restricted", is_member=False)))
        self.assertFalse(bot.is_channel_member(SimpleNamespace(status="left")))

    def test_reopened_human_check_repeats_the_same_question(self) -> None:
        first_question, first_keyboard = bot.human_challenge(101, "zh")
        next_question, next_keyboard = bot.human_challenge(101, "zh")
        self.assertEqual(first_question, next_question)
        self.assertIn(" + ", next_question)
        self.assertEqual(first_keyboard, next_keyboard)

    async def test_first_use_requires_human_check_then_official_channel_membership(self) -> None:
        message = FakeGateMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=101),
            effective_chat=SimpleNamespace(type="private"),
            effective_message=message,
        )
        gate_bot = FakeGateBot()
        self.assertFalse(await bot.require_private(update, gate_bot))
        self.assertIn("人机验证", message.replies[0][0])
        self.assertEqual(gate_bot.calls, [])

        self.store.mark_human_verified(101)
        self.assertTrue(await bot.require_private(update, gate_bot))
        self.assertEqual(gate_bot.calls, [("@dro8r", 101)])

        not_joined_bot = FakeGateBot(status="left")
        self.assertFalse(await bot.require_private(update, not_joined_bot))
        self.assertIn("官方频道", message.replies[-1][0])

    async def test_auto_reply_callback_shows_onboarding_without_unauthorized_alert(self) -> None:
        message = FakeGateMessage()
        query = FakeGateQuery("auto:acct:1", message)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=101),
            effective_chat=SimpleNamespace(type="private"),
            effective_message=message,
            callback_query=query,
        )
        await bot.auto_reply_callback(update, SimpleNamespace(bot=FakeGateBot()))
        self.assertIn("人机验证", message.replies[0][0])
        self.assertFalse(any(kwargs.get("show_alert") for _args, kwargs in query.answers))

    async def test_correct_first_use_challenge_unlocks_verified_channel_member(self) -> None:
        message = FakeGateMessage()
        prompt, _keyboard = bot.human_challenge(101, "zh")
        answer = bot.pending_human_challenges[101][0]
        query = FakeGateQuery(f"human:{answer}", message)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=101),
            effective_chat=SimpleNamespace(type="private"),
            effective_message=message,
            callback_query=query,
        )
        gate_bot = FakeGateBot(status="member")
        await bot.human_captcha_callback(update, SimpleNamespace(bot=gate_bot))
        self.assertTrue(self.store.is_human_verified(101))
        self.assertEqual(gate_bot.calls, [("@dro8r", 101)])
        self.assertIn("人机验证和频道验证都已通过", message.replies[-1][0])
        self.assertTrue(prompt)

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
