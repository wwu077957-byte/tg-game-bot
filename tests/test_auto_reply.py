from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from tg_manager.auto_reply import (
    choose_scene,
    default_config,
    in_time_window,
    normalize_config,
    offline_reason,
    render_template,
)
from tg_manager.storage import Store


class AutoReplyLogicTests(unittest.TestCase):
    def test_time_window_supports_overnight_and_disabled_windows(self) -> None:
        self.assertTrue(in_time_window(23, 23, 9))
        self.assertTrue(in_time_window(8, 23, 9))
        self.assertFalse(in_time_window(9, 23, 9))
        self.assertFalse(in_time_window(8, 9, 23))
        self.assertFalse(in_time_window(23, 23, 23))

    def test_offline_triggers_are_or_and_takeover_overrides_them(self) -> None:
        config = normalize_config(default_config())
        config.update({"start_hour": 0, "end_hour": 0, "idle_minutes": 15, "queue_threshold": 3})
        base = dict(
            config=config,
            enabled=True,
            manual_auto=False,
            last_human_at=10_000,
            paused_until=0,
            pending_count=0,
            now=10_500,
        )
        self.assertIsNone(offline_reason(**base))
        self.assertEqual(offline_reason(**{**base, "pending_count": 4}), "queue")
        self.assertEqual(offline_reason(**{**base, "now": 10_900}), "idle")
        self.assertEqual(offline_reason(**{**base, "manual_auto": True}), "manual")
        night_config = {**config, "start_hour": 23, "end_hour": 9, "tz_offset": 0}
        night_now = datetime(2026, 11, 26, 23, 30, tzinfo=timezone.utc).timestamp()
        self.assertEqual(offline_reason(**{**base, "config": night_config, "now": night_now}), "schedule")
        self.assertIsNone(offline_reason(**{**base, "pending_count": 9, "paused_until": 10_600}))
        self.assertIsNone(offline_reason(**{**base, "enabled": False, "manual_auto": True}))

    def test_scene_priority_and_supported_placeholders(self) -> None:
        config = normalize_config(default_config())
        config.update({"start_hour": 0, "end_hour": 0, "product": "TGVault"})
        now = datetime(2026, 11, 26, 12, 5, tzinfo=timezone.utc).timestamp()
        self.assertEqual(choose_scene(config, first_contact=True, now=now), "welcome")
        self.assertEqual(choose_scene(config, first_contact=False, manual_auto=True, now=now), "offline")
        config["scene_mode"] = "sales"
        self.assertEqual(choose_scene(config, first_contact=False, now=now), "sales")
        text = render_template("Hi {name}, {product} at {time} {bad}", name="Ada", now=now, config=config)
        self.assertEqual(text, "Hi Ada, TGVault at 20:05")

    def test_runtime_account_state_is_not_part_of_template_configuration(self) -> None:
        config = normalize_config({
            **default_config(),
            "account_id": 99,
            "enabled": True,
            "manual_auto": True,
            "pending_count": 7,
        })
        self.assertNotIn("account_id", config)
        self.assertNotIn("enabled", config)
        self.assertNotIn("manual_auto", config)
        self.assertNotIn("pending_count", config)


class AutoReplyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "auto.sqlite3"
        self.key = Fernet.generate_key().decode("ascii")
        self.store = Store(str(self.db), self.key)
        self.account_id = self.store.add_account(101, "+15551234567", "", "encrypted-session")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def enable(self, now: float = 1_000.0) -> None:
        self.assertTrue(self.store.set_auto_reply_enabled(101, self.account_id, True, manual_auto=False, now=now))

    def test_new_account_auto_reply_is_disabled_by_default(self) -> None:
        config = self.store.get_auto_reply_settings(101, self.account_id)
        self.assertFalse(config["enabled"])
        self.assertFalse(config["manual_auto"])
        self.assertEqual(self.store.active_auto_reply_account_ids(101), [])
        incoming = self.store.record_auto_reply_incoming(101, self.account_id, 7001, 10, now=1_100)
        self.assertFalse(incoming["accepted"])

    def test_configuration_is_encrypted_persistent_and_owner_scoped(self) -> None:
        self.enable()
        config = self.store.get_auto_reply_settings(101, self.account_id)
        assert config is not None
        config["product"] = "private-product-name"
        config["scenes"]["welcome"] = "private-welcome-template"
        self.store.save_auto_reply_config(101, self.account_id, config)
        self.assertIsNone(self.store.get_auto_reply_settings(202, self.account_id))
        self.assertFalse(self.store.save_auto_reply_config(202, self.account_id, config))
        with sqlite3.connect(self.db) as conn:
            raw = conn.execute("SELECT config_cipher FROM auto_reply_settings WHERE account_id=?", (self.account_id,)).fetchone()[0]
        self.assertNotIn(b"private-product-name", raw)
        self.assertNotIn(b"private-welcome-template", raw)
        reopened = Store(str(self.db), self.key)
        restored = reopened.get_auto_reply_settings(101, self.account_id)
        self.assertEqual(restored["product"], "private-product-name")
        self.assertEqual(restored["scenes"]["welcome"], "private-welcome-template")

    def test_incoming_queue_is_deduplicated_and_human_reply_clears_it(self) -> None:
        self.enable()
        first = self.store.record_auto_reply_incoming(101, self.account_id, 7001, 10, now=1_100)
        second = self.store.record_auto_reply_incoming(101, self.account_id, 7001, 11, now=1_101)
        duplicate = self.store.record_auto_reply_incoming(101, self.account_id, 7001, 11, now=1_102)
        other = self.store.record_auto_reply_incoming(101, self.account_id, 7002, 20, now=1_103)
        self.assertEqual(first, {"accepted": True, "first_contact": True, "pending_count": 1})
        self.assertEqual(second, {"accepted": True, "first_contact": False, "pending_count": 2})
        self.assertFalse(duplicate["accepted"])
        self.assertEqual(other["pending_count"], 3)
        self.assertTrue(self.store.record_auto_reply_human_outgoing(101, self.account_id, 7001, 12, now=1_200))
        self.assertEqual(self.store.auto_reply_pending_count(101, self.account_id), 1)
        state = self.store.get_auto_reply_settings(101, self.account_id)
        self.assertFalse(state["manual_auto"])
        self.assertEqual(state["last_human_at"], 1_200)
        self.assertEqual(state["paused_until"], 2_100)
        self.assertFalse(self.store.record_auto_reply_human_outgoing(101, self.account_id, 7001, 12, now=1_500))
        state = self.store.get_auto_reply_settings(101, self.account_id)
        self.assertEqual(state["last_human_at"], 1_200)
        self.assertEqual(state["paused_until"], 2_100)

    def test_cooldown_daily_cap_and_auto_marker(self) -> None:
        self.enable()
        self.store.record_auto_reply_incoming(101, self.account_id, 7001, 10, now=10_000)
        self.assertTrue(self.store.claim_auto_reply(101, self.account_id, 7001, cooldown_seconds=1800,
                                                    daily_cap=2, local_day="20261126", now=10_000))
        self.assertFalse(self.store.claim_auto_reply(101, self.account_id, 7001, cooldown_seconds=1800,
                                                     daily_cap=2, local_day="20261126", now=10_100))
        self.assertTrue(self.store.record_auto_reply_sent(101, self.account_id, 7001, 900, "night", now=10_001))
        self.assertTrue(self.store.is_auto_reply_message(101, self.account_id, 7001, 900))
        self.assertFalse(self.store.is_auto_reply_message(202, self.account_id, 7001, 900))
        self.assertEqual(self.store.list_auto_reply_messages(101, self.account_id)[0]["scene"], "night")
        self.assertEqual(self.store.auto_reply_pending_count(101, self.account_id), 0)

    def test_manual_all_and_takeover_state_are_persistent_and_account_scoped(self) -> None:
        second_id = self.store.add_account(101, "+15551234568", "", "another-session")
        self.assertEqual(self.store.set_auto_reply_all(101, True, now=5_000), 2)
        first = self.store.get_auto_reply_settings(101, self.account_id)
        second = self.store.get_auto_reply_settings(101, second_id)
        self.assertTrue(first["enabled"] and first["manual_auto"])
        self.assertTrue(second["enabled"] and second["manual_auto"])
        self.assertTrue(self.store.record_auto_reply_human_outgoing(101, self.account_id, 7001, 51, now=5_100))
        first = self.store.get_auto_reply_settings(101, self.account_id)
        second = self.store.get_auto_reply_settings(101, second_id)
        self.assertFalse(first["manual_auto"])
        self.assertGreater(first["paused_until"], 5_100)
        self.assertTrue(second["manual_auto"])
        self.assertEqual(self.store.set_auto_reply_all(101, False, now=5_200), 2)
        self.assertFalse(self.store.get_auto_reply_settings(101, self.account_id)["enabled"])

    def test_v2_database_upgrade_does_not_reencrypt_existing_metadata(self) -> None:
        self.enable()
        config = self.store.get_auto_reply_settings(101, self.account_id)
        config["product"] = "keep-encrypted-config"
        self.store.save_auto_reply_config(101, self.account_id, config)
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA user_version = 2")
        upgraded = Store(str(self.db), self.key)
        result = upgraded.get_auto_reply_settings(101, self.account_id)
        self.assertEqual(result["product"], "keep-encrypted-config")
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
