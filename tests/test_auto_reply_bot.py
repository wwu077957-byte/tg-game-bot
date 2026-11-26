from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
        self.next_message_id = 900

    async def send_message(self, peer_id: int, text: str):
        self.sent.append((peer_id, text))
        message = SimpleNamespace(id=self.next_message_id)
        self.next_message_id += 1
        return message


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


class FakeNotificationBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object):
        self.sent.append(dict(kwargs))
        return SimpleNamespace(message_id=len(self.sent))

    async def delete_message(self, **_kwargs: object) -> None:
        return None


class FakeAuthorizationClient:
    def __init__(self, authorizations: list[object]) -> None:
        self.authorizations = authorizations

    async def __call__(self, _request: object):
        return SimpleNamespace(authorizations=self.authorizations)


class FakeGateQuery:
    def __init__(self, data: str, message: FakeGateMessage) -> None:
        self.data = data
        self.message = message
        self.answers: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def answer(self, *args: object, **kwargs: object) -> None:
        self.answers.append((args, dict(kwargs)))


class FakeFallbackMessage(FakeGateMessage):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def reply_text(self, text: str, **kwargs: object) -> None:
        self.calls.append((text, dict(kwargs)))
        if len(self.calls) == 1:
            from telegram.error import BadRequest

            raise BadRequest("Bad Request: custom emoji unavailable")
        self.replies.append((text, dict(kwargs)))


class AutoReplyBotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_store = bot.store
        self.previous_custom_emoji_enabled = bot.CUSTOM_EMOJI_ENABLED
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
        bot.CUSTOM_EMOJI_ENABLED = False

    def tearDown(self) -> None:
        bot.store = self.previous_store
        bot.auto_reply_locks.clear()
        bot.auto_sending_peers.clear()
        bot.pending_human_challenges.clear()
        bot.CUSTOM_EMOJI_ENABLED = self.previous_custom_emoji_enabled
        self.tmp.cleanup()

    def test_account_labels_follow_language(self) -> None:
        row = self.store.get_account(101, self.account_id)
        row.update({"label": "Work", "group_name": "Friends"})
        self.assertIn("Group: Friends", bot.format_account(row, "en"))
        self.assertIn("分组：Friends", bot.format_account(row, "zh"))

    def test_account_list_links_display_name_and_offers_username_id_button(self) -> None:
        row = self.store.get_account(101, self.account_id)
        text, entities, keyboard = bot.build_account_list(
            [row],
            [{"id": 8351483076, "username": "v8erx", "display_name": "Nora 🐈"}],
            "en",
        )

        self.assertIn("#1", text)
        self.assertIn("Nora 🐈", text)
        self.assertEqual(len(entities), 1)
        entity = entities[0]
        name_start = text.index("Nora 🐈")
        self.assertEqual(entity.offset, bot.utf16_code_units(text[:name_start]))
        self.assertEqual(entity.length, bot.utf16_code_units("Nora 🐈"))
        self.assertEqual(entity.url, "tg://user?id=8351483076")
        button = keyboard.inline_keyboard[0][0]
        self.assertEqual(button.callback_data, f"acctinfo:{self.account_id}")
        self.assertIn("Username & ID", button.text)

    async def test_account_info_callback_shows_username_id_and_profile_link(self) -> None:
        self.store.mark_human_verified(101)
        message = FakeGateMessage()
        query = FakeGateQuery(f"acctinfo:{self.account_id}", message)
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=101),
            effective_chat=SimpleNamespace(type="private"),
            effective_message=message,
        )
        identity = {"id": 8351483076, "username": "v8erx", "display_name": "Nora 🐈"}
        with patch.object(bot, "read_account_identity", new=AsyncMock(return_value=identity)):
            await bot.account_info_callback(update, SimpleNamespace(bot=FakeGateBot("member")))

        self.assertEqual(len(message.replies), 1)
        text, kwargs = message.replies[0]
        self.assertIn("@v8erx", text)
        self.assertIn("8351483076", text)
        self.assertEqual(kwargs["entities"][0].url, "tg://user?id=8351483076")
        self.assertEqual(len(query.answers), 1)

    async def test_account_info_callback_rejects_non_owner(self) -> None:
        message = FakeGateMessage()
        query = FakeGateQuery(f"acctinfo:{self.account_id}", message)
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=202),
            effective_chat=SimpleNamespace(type="private"),
            effective_message=message,
        )
        lookup = AsyncMock(return_value={"id": 8351483076, "username": "v8erx", "display_name": "Nora"})
        with patch.object(bot, "read_account_identity", lookup):
            await bot.account_info_callback(update, SimpleNamespace(bot=FakeGateBot("member")))

        lookup.assert_not_awaited()
        self.assertFalse(message.replies)
        self.assertTrue(query.answers[0][1].get("show_alert"))

    async def test_profile_link_entity_coexists_with_custom_emoji(self) -> None:
        bot.CUSTOM_EMOJI_ENABLED = True
        text = "👤 Nora 🐈"
        start = text.index("Nora")
        link = bot.profile_text_link(text, start, len(text), 8351483076)
        message = FakeGateMessage()

        await bot.reply_ui_text(message, text, entities=[link])

        entities = message.replies[0][1]["entities"]
        self.assertEqual(len(entities), 2)
        self.assertEqual(entities[0].type, bot.MessageEntity.TEXT_LINK)
        self.assertEqual(entities[0].url, "tg://user?id=8351483076")
        self.assertEqual(entities[1].type, bot.MessageEntity.CUSTOM_EMOJI)

    def test_auto_reply_scene_labels_use_language_without_changing_templates(self) -> None:
        english = [button.text for row in bot.auto_scene_keyboard(1, "en").inline_keyboard for button in row]
        chinese = [button.text for row in bot.auto_scene_keyboard(1, "zh").inline_keyboard for button in row]
        self.assertIn("👤 First contact/welcome", english)
        self.assertIn("👤 首次接触/欢迎语", chinese)
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

    async def test_every_inbound_message_is_replied_to_after_idle_without_queue_or_rate_caps(self) -> None:
        self.store.set_auto_reply_enabled(101, self.account_id, True, manual_auto=False, now=20_000)
        config = self.store.get_auto_reply_settings(101, self.account_id)
        config.update({
            "idle_minutes": 1,
            "queue_threshold": 0,
            "start_hour": 0,
            "end_hour": 0,
            "cooldown_seconds": 0,
            "daily_cap": 0,
            "simple_reply": "Received; I will get back to you soon.",
        })
        self.store.save_auto_reply_config(101, self.account_id, config)
        client = FakeClient()

        async def no_sleep(_seconds: float) -> None:
            return None

        with patch.object(bot.asyncio, "sleep", new=no_sleep):
            await bot.handle_auto_reply_event(FakeEvent(peer_id=7001, message_id=20), 101, self.account_id, client)
            await bot.handle_auto_reply_event(FakeEvent(peer_id=7001, message_id=21), 101, self.account_id, client)
        self.assertEqual(client.sent, [
            (7001, "Received; I will get back to you soon."),
            (7001, "Received; I will get back to you soon."),
        ])

    async def test_context_ai_is_used_only_for_existing_private_chats(self) -> None:
        self.store.set_auto_reply_enabled(101, self.account_id, True, manual_auto=False, now=20_000)
        config = self.store.get_auto_reply_settings(101, self.account_id)
        config.update({
            "idle_minutes": 1,
            "queue_threshold": 0,
            "start_hour": 0,
            "end_hour": 0,
            "cooldown_seconds": 0,
            "daily_cap": 0,
            "simple_reply": "Fixed message for new contacts.",
            "contextual_replies": True,
        })
        self.store.save_auto_reply_config(101, self.account_id, config)
        client = FakeClient()
        generate = AsyncMock(return_value="Context-aware reply.")
        recent = AsyncMock(return_value=[{"role": "user", "content": "Can we meet tomorrow?"}])

        async def no_sleep(_seconds: float) -> None:
            return None

        with (
            patch.object(bot.asyncio, "sleep", new=no_sleep),
            patch.object(bot, "generate_contextual_reply", generate),
            patch.object(bot, "recent_private_context", recent),
            patch.object(bot, "DEEPSEEK_API_KEY", "unit-test-key"),
        ):
            await bot.handle_auto_reply_event(FakeEvent(peer_id=7100, message_id=1), 101, self.account_id, client)
            await bot.handle_auto_reply_event(FakeEvent(peer_id=7100, message_id=2), 101, self.account_id, client)

        self.assertEqual(client.sent, [
            (7100, "Fixed message for new contacts."),
            (7100, "Context-aware reply."),
        ])
        generate.assert_awaited_once()
        recent.assert_awaited_once()

    async def test_owner_private_and_auto_reply_alerts_exclude_message_text(self) -> None:
        self.store.set_language(101, "en")
        self.store.set_auto_reply_enabled(101, self.account_id, True, manual_auto=False, now=20_000)
        config = self.store.get_auto_reply_settings(101, self.account_id)
        config.update({
            "idle_minutes": 1,
            "queue_threshold": 0,
            "start_hour": 0,
            "end_hour": 0,
            "cooldown_seconds": 0,
            "daily_cap": 0,
            "simple_reply": "Received.",
            "notify_private_messages": True,
            "notify_auto_reply_sent": True,
        })
        self.store.save_auto_reply_config(101, self.account_id, config)
        event = FakeEvent(peer_id=7200, message_id=8)
        event.message.message = "PRIVATE BODY with login code 847293"
        client = FakeClient()
        owner_bot = FakeNotificationBot()
        application = SimpleNamespace(bot=owner_bot)

        async def no_sleep(_seconds: float) -> None:
            return None

        with patch.object(bot.asyncio, "sleep", new=no_sleep):
            await bot.handle_auto_reply_event(event, 101, self.account_id, client, application)

        self.assertEqual(len(owner_bot.sent), 2)
        notifications = [str(item["text"]) for item in owner_bot.sent]
        self.assertIn("new private message", notifications[0])
        self.assertIn("sent an automatic reply", notifications[1])
        for notification in notifications:
            self.assertNotIn("PRIVATE BODY", notification)
            self.assertNotIn("847293", notification)
        self.assertTrue(all(item["chat_id"] == 101 for item in owner_bot.sent))

    async def test_new_device_check_sets_silent_baseline_then_alerts_owner(self) -> None:
        self.store.set_language(101, "en")
        config = self.store.get_auto_reply_settings(101, self.account_id)
        config["notify_new_device"] = True
        self.store.save_auto_reply_config(101, self.account_id, config)
        client = FakeAuthorizationClient([
            SimpleNamespace(hash=111, device_model="Chrome 152", app_name="Telegram Web", app_version="1.0", platform="Web", ip="203.0.113.4", country="Example")
        ])
        owner_bot = FakeNotificationBot()
        application = SimpleNamespace(bot=owner_bot)

        self.assertFalse(await bot.check_new_device_authorizations(client, 101, self.account_id, application))
        self.assertEqual(owner_bot.sent, [])
        client.authorizations.append(
            SimpleNamespace(hash=222, device_model="Firefox 150", app_name="Telegram Web", app_version="1.0", platform="Web", ip="198.51.100.7", country="Example")
        )
        self.assertTrue(await bot.check_new_device_authorizations(client, 101, self.account_id, application))
        self.assertEqual(len(owner_bot.sent), 1)
        text = str(owner_bot.sent[0]["text"])
        self.assertIn("new login", text)
        self.assertIn("Firefox 150", text)
        self.assertNotIn("198.51.100.7", text)
        self.assertNotIn("Example", text)

    async def test_custom_emoji_permission_failure_retries_with_unicode_fallback(self) -> None:
        bot.CUSTOM_EMOJI_ENABLED = True
        markup = bot.InlineKeyboardMarkup(
            [[bot.make_inline_button("🔔 Alerts", callback_data="alerts")]]
        )
        message = FakeFallbackMessage()

        await bot.reply_ui_text(message, "🔔 Alerts are ready.", reply_markup=markup)

        self.assertEqual(len(message.calls), 2)
        self.assertTrue(message.calls[0][1].get("entities"))
        self.assertEqual(message.replies[0][0], "🔔 Alerts are ready.")
        fallback_button = message.replies[0][1]["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(fallback_button.text, "🔔 Alerts")
        self.assertFalse(getattr(fallback_button, "icon_custom_emoji_id", None))
        self.assertFalse(bot.CUSTOM_EMOJI_ENABLED)

    async def test_manual_outgoing_message_immediately_pauses_auto_replies(self) -> None:
        self.store.set_auto_reply_enabled(101, self.account_id, True, manual_auto=False, now=20_000)
        config = self.store.get_auto_reply_settings(101, self.account_id)
        config.update({"idle_minutes": 1, "queue_threshold": 0, "start_hour": 0, "end_hour": 0,
                       "cooldown_seconds": 0, "daily_cap": 0, "simple_reply": "Received."})
        self.store.save_auto_reply_config(101, self.account_id, config)
        client = FakeClient()

        async def no_sleep(_seconds: float) -> None:
            return None

        with patch.object(bot.asyncio, "sleep", new=no_sleep):
            await bot.handle_auto_reply_event(FakeEvent(peer_id=7001, message_id=30), 101, self.account_id, client)
            await bot.handle_auto_reply_event(
                FakeEvent(peer_id=7002, message_id=31, outgoing=True), 101, self.account_id, client
            )
            await bot.handle_auto_reply_event(FakeEvent(peer_id=7001, message_id=32), 101, self.account_id, client)
        self.assertEqual(client.sent, [(7001, "Received.")])

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
