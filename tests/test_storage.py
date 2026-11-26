from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from tg_manager.storage import (
    AUTH_LOCKOUT_SECONDS,
    AUTH_PASSWORD_FAILURE_LIMIT,
    PHONE_CODE_MIN_INTERVAL_SECONDS,
    PHONE_CODE_REQUEST_LIMIT,
    Store,
)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "private.sqlite3"
        self.key = Fernet.generate_key().decode()
        self.store = Store(str(self.db), self.key)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_session_and_account_access_are_owner_scoped(self) -> None:
        session = "telethon-session-secret-for-test"
        account_id = self.store.add_account(101, "+15551234567", "personal", session)
        self.assertEqual(self.store.get_session(101, account_id), session)
        self.assertIsNone(self.store.get_session(202, account_id))
        self.assertIsNone(self.store.get_account(202, account_id))
        self.assertEqual(self.store.list_accounts(202), [])
        with sqlite3.connect(self.db) as conn:
            cipher = conn.execute("SELECT session_cipher FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
        self.assertNotIn(session.encode(), cipher)

    def test_phone_notes_groups_and_audit_text_are_encrypted_at_rest(self) -> None:
        phone = "+15551234567"
        note = "private-note-plaintext"
        group_name = "friends-group-plaintext"
        audit_detail = "audit-detail-plaintext"
        account_id = self.store.add_account(101, phone, note, "session")
        self.assertTrue(self.store.create_group(101, group_name))
        self.assertTrue(self.store.move_to_group(101, account_id, group_name))
        self.store.audit(101, "custom_action_plaintext", account_id, audit_detail)

        rows = []
        with sqlite3.connect(self.db) as conn:
            rows.extend(conn.execute("SELECT phone, label FROM accounts WHERE id=?", (account_id,)).fetchone())
            rows.extend(conn.execute("SELECT name FROM groups WHERE owner_id=?", (101,)).fetchone())
            rows.extend(conn.execute("SELECT action, detail FROM audit ORDER BY id DESC LIMIT 1").fetchone())
        stored = [bytes(value) if isinstance(value, (bytes, bytearray)) else str(value).encode() for value in rows]
        for secret in (phone, note, group_name, audit_detail, "custom_action_plaintext"):
            self.assertTrue(all(secret.encode() not in value for value in stored), secret)

        row = self.store.list_accounts(101)[0]
        self.assertEqual(row["phone"], phone)
        self.assertEqual(row["label"], note)
        self.assertEqual(row["group_name"], group_name)
        self.assertEqual(self.store.list_groups(101), [{"id": 1, "name": group_name}])
        self.assertEqual(self.store.list_audit(101)[0]["detail"], audit_detail)

    def test_soft_delete_restore_is_owner_scoped(self) -> None:
        account_id = self.store.add_account(101, "+15551234567", "", "session")
        self.assertTrue(self.store.delete_account(101, account_id))
        self.assertIsNone(self.store.get_account(101, account_id))
        self.assertFalse(self.store.restore_account(202, account_id))
        self.assertTrue(self.store.restore_account(101, account_id))
        self.assertIsNotNone(self.store.get_account(101, account_id))

    def test_groups_and_audit_are_owner_scoped(self) -> None:
        account_id = self.store.add_account(101, "+15551234567", "", "session")
        self.assertTrue(self.store.create_group(101, "Friends"))
        self.assertTrue(self.store.move_to_group(101, account_id, "Friends"))
        row = self.store.list_accounts(101)[0]
        self.assertEqual(row["group_name"], "Friends")
        self.assertEqual(self.store.list_groups(202), [])
        self.assertGreaterEqual(len(self.store.list_audit(101)), 3)
        self.assertEqual(self.store.list_audit(202), [])

    def test_language_preference_defaults_to_chinese_and_persists_per_user(self) -> None:
        self.assertEqual(self.store.get_language(101), "zh")
        self.store.set_language(101, "en")
        reopened = Store(str(self.db), self.key)
        self.assertEqual(reopened.get_language(101), "en")
        self.assertEqual(reopened.get_language(202), "zh")
        with self.assertRaises(ValueError):
            self.store.set_language(101, "fr")

    def test_human_verification_is_required_once_and_persists_per_user(self) -> None:
        self.assertFalse(self.store.is_human_verified(101))
        self.store.mark_human_verified(101, now=1234)
        reopened = Store(str(self.db), self.key)
        self.assertTrue(reopened.is_human_verified(101))
        self.assertFalse(reopened.is_human_verified(202))

    def test_code_and_password_failures_have_separate_persistent_cooldowns(self) -> None:
        now = 10_000
        for attempt in range(5):
            attempt_time = now + (attempt * 5)
            self.assertEqual(self.store.begin_auth_attempt(101, "code", now=attempt_time), 0)
            remaining = self.store.record_auth_failure(101, "code", now=attempt_time)
        self.assertEqual(remaining, AUTH_LOCKOUT_SECONDS)
        self.assertEqual(self.store.begin_auth_attempt(101, "code", now=now + 20), AUTH_LOCKOUT_SECONDS)
        self.assertEqual(self.store.begin_auth_attempt(101, "password", now=now + 20), 0)
        self.assertEqual(self.store.begin_auth_attempt(202, "code", now=now + 20), 0)

        reopened = Store(str(self.db), self.key)
        self.assertEqual(reopened.begin_auth_attempt(101, "code", now=now + 21), AUTH_LOCKOUT_SECONDS - 1)
        self.assertEqual(reopened.begin_auth_attempt(101, "code", now=now + 20 + AUTH_LOCKOUT_SECONDS), 0)

    def test_existing_global_lockout_is_honored_for_new_scoped_login(self) -> None:
        now = 15_000
        for attempt in range(5):
            current = now + attempt * 5
            self.assertEqual(self.store.begin_auth_attempt(101, "code", now=current), 0)
            remaining = self.store.record_auth_failure(101, "code", now=current)
        self.assertEqual(remaining, AUTH_LOCKOUT_SECONDS)
        self.assertEqual(
            self.store.begin_auth_attempt(101, "code", now=now + 20, account_phone="+447700900123"),
            AUTH_LOCKOUT_SECONDS,
        )
        self.assertEqual(
            self.store.begin_auth_attempt(
                101, "code", now=now + 20 + AUTH_LOCKOUT_SECONDS, account_phone="+447700900123"
            ),
            0,
        )

    def test_wrong_attempts_are_spaced_and_success_can_clear_throttle(self) -> None:
        now = 20_000
        self.assertEqual(self.store.begin_auth_attempt(101, "password", now=now), 0)
        self.store.record_auth_failure(101, "password", now=now)
        self.assertEqual(self.store.begin_auth_attempt(101, "password", now=now + 1), 4)
        self.assertEqual(self.store.begin_auth_attempt(101, "password", now=now + 5), 0)
        self.store.clear_auth_attempts(101, "password")
        self.assertEqual(self.store.begin_auth_attempt(101, "password", now=now + 6), 0)

    def test_password_is_locked_after_three_failures_and_scoped_by_phone(self) -> None:
        now = 30_000
        phone_a = "+14155552671"
        phone_b = "+14165550123"
        for attempt in range(AUTH_PASSWORD_FAILURE_LIMIT):
            current = now + attempt * 5
            self.assertEqual(self.store.begin_auth_attempt(101, "password", now=current, account_phone=phone_a), 0)
            remaining = self.store.record_auth_failure(101, "password", now=current, account_phone=phone_a)
        self.assertEqual(remaining, AUTH_LOCKOUT_SECONDS)
        self.assertEqual(
            self.store.begin_auth_attempt(
                101, "password", now=now + (AUTH_PASSWORD_FAILURE_LIMIT - 1) * 5, account_phone=phone_a
            ),
            AUTH_LOCKOUT_SECONDS,
        )
        self.assertEqual(self.store.begin_auth_attempt(101, "password", now=now + 15, account_phone=phone_b), 0)
        self.assertEqual(self.store.begin_auth_attempt(202, "password", now=now + 15, account_phone=phone_a), 0)

    def test_code_failure_limit_is_five_and_scoped_by_phone(self) -> None:
        now = 35_000
        phone_a = "+14155552671"
        phone_b = "+14165550123"
        for attempt in range(5):
            current = now + attempt * 5
            self.assertEqual(self.store.begin_auth_attempt(101, "code", now=current, account_phone=phone_a), 0)
            remaining = self.store.record_auth_failure(101, "code", now=current, account_phone=phone_a)
        self.assertEqual(remaining, AUTH_LOCKOUT_SECONDS)
        self.assertEqual(self.store.begin_auth_attempt(101, "code", now=now + 20, account_phone=phone_a), AUTH_LOCKOUT_SECONDS)
        self.assertEqual(self.store.begin_auth_attempt(101, "code", now=now + 20, account_phone=phone_b), 0)

    def test_phone_code_requests_have_persistent_per_number_cooldown_and_four_resend_cap(self) -> None:
        now = 40_000
        phone_a = "+14155552671"
        phone_b = "+14165550123"
        for attempt in range(PHONE_CODE_REQUEST_LIMIT):
            current = now + attempt * PHONE_CODE_MIN_INTERVAL_SECONDS
            wait, used = self.store.reserve_phone_code_request(101, phone_a, now=current)
            self.assertEqual((wait, used), (0, attempt + 1))

        wait, used = self.store.reserve_phone_code_request(
            101, phone_a, now=now + PHONE_CODE_REQUEST_LIMIT * PHONE_CODE_MIN_INTERVAL_SECONDS
        )
        self.assertGreater(wait, 0)
        self.assertEqual(used, PHONE_CODE_REQUEST_LIMIT)
        self.assertEqual(self.store.reserve_phone_code_request(101, phone_b, now=now), (0, 1))
        self.assertEqual(self.store.reserve_phone_code_request(202, phone_a, now=now), (0, 1))

        reopened = Store(str(self.db), self.key)
        wait, used = reopened.reserve_phone_code_request(101, phone_a, now=now + 300)
        self.assertGreater(wait, 0)
        self.assertEqual(used, PHONE_CODE_REQUEST_LIMIT)

    def test_phone_request_cooldown_honors_server_flood_wait_without_plaintext_phone(self) -> None:
        phone = "+8613800138000"
        now = 50_000
        self.assertEqual(self.store.reserve_phone_code_request(101, phone, now=now), (0, 1))
        self.store.defer_phone_code_request(101, phone, 240, now=now)
        self.assertEqual(self.store.reserve_phone_code_request(101, phone, now=now + 45), (195, 1))
        with sqlite3.connect(self.db) as conn:
            row = conn.execute("SELECT phone_key FROM phone_code_limits WHERE owner_id = 101").fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn(phone.encode(), str(row[0]).encode())

    def test_long_server_flood_wait_survives_window_reset_and_cleanup(self) -> None:
        now = 55_000
        phone_a = "+447700900123"
        phone_b = "+447700900456"
        self.assertEqual(self.store.reserve_phone_code_request(101, phone_a, now=now), (0, 1))
        self.store.defer_phone_code_request(101, phone_a, 48 * 60 * 60, now=now)

        later = now + 24 * 60 * 60 + 1
        wait, attempts = self.store.reserve_phone_code_request(101, phone_a, now=later)
        self.assertGreater(wait, 0)
        self.assertEqual(attempts, 0)
        self.assertEqual(self.store.reserve_phone_code_request(101, phone_b, now=later), (0, 1))
        with sqlite3.connect(self.db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM phone_code_limits WHERE owner_id = 101").fetchone()[0]
        self.assertEqual(count, 2)

    def test_server_flood_wait_is_persistent_and_scoped_to_one_login(self) -> None:
        now = 60_000
        phone_a = "+14155552671"
        phone_b = "+14165550123"
        self.store.defer_auth_attempt(101, "code", 120, account_phone=phone_a, now=now)
        self.assertEqual(self.store.begin_auth_attempt(101, "code", now=now + 30, account_phone=phone_a), 90)
        self.assertEqual(self.store.begin_auth_attempt(101, "code", now=now + 30, account_phone=phone_b), 0)
        reopened = Store(str(self.db), self.key)
        self.assertEqual(reopened.begin_auth_attempt(101, "code", now=now + 60, account_phone=phone_a), 60)
        self.assertEqual(reopened.begin_auth_attempt(101, "code", now=now + 120, account_phone=phone_a), 0)

    def test_legacy_plaintext_metadata_is_migrated(self) -> None:
        legacy_db = Path(self.tmp.name) / "legacy.sqlite3"
        session_cipher = Fernet(self.key.encode()).encrypt(b"old-session")
        with sqlite3.connect(legacy_db) as conn:
            conn.executescript(
                """
                CREATE TABLE groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(owner_id, name)
                );
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    phone TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    group_id INTEGER,
                    session_cipher BLOB NOT NULL,
                    created_at INTEGER NOT NULL,
                    deleted_at INTEGER,
                    UNIQUE(owner_id, phone),
                    FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE SET NULL
                );
                CREATE TABLE audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    account_id INTEGER,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                );
                """
            )
            conn.execute("INSERT INTO groups(owner_id, name, created_at) VALUES(101, 'Old Group', 1)")
            conn.execute(
                "INSERT INTO accounts(owner_id, phone, label, group_id, session_cipher, created_at) "
                "VALUES(101, '+15550001111', 'Old Note', 1, ?, 1)",
                (session_cipher,),
            )
            conn.execute("INSERT INTO audit(owner_id, action, detail, created_at) VALUES(101, 'old_action', 'old_detail', 1)")

        migrated = Store(str(legacy_db), self.key)
        self.assertEqual(migrated.list_accounts(101)[0]["phone"], "+15550001111")
        self.assertEqual(migrated.list_accounts(101)[0]["label"], "Old Note")
        self.assertEqual(migrated.list_groups(101)[0]["name"], "Old Group")
        self.assertEqual(migrated.list_audit(101)[0]["detail"], "old_detail")
        with sqlite3.connect(legacy_db) as conn:
            phone, label = conn.execute("SELECT phone, label FROM accounts").fetchone()
            action, detail = conn.execute("SELECT action, detail FROM audit").fetchone()
            group_name = conn.execute("SELECT name FROM groups").fetchone()[0]
        for plain in (b"+15550001111", b"Old Note", b"Old Group", b"old_action", b"old_detail"):
            self.assertNotIn(plain, (phone, label, action, detail, group_name))

    def test_authorization_snapshot_is_owner_scoped_and_stores_only_fingerprints(self) -> None:
        account_id = self.store.add_account(101, "+15551234567", "", "session")
        self.assertEqual(self.store.update_authorization_snapshot(101, account_id, [99887766]), set())
        self.assertEqual(self.store.update_authorization_snapshot(101, account_id, [99887766]), set())
        self.assertEqual(self.store.update_authorization_snapshot(202, account_id, [11223344]), set())
        self.assertEqual(self.store.update_authorization_snapshot(101, account_id, [99887766, 11223344]), {11223344})
        with sqlite3.connect(self.db) as conn:
            stored = conn.execute(
                "SELECT fingerprints_json FROM telegram_authorization_snapshots WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0]
        self.assertNotIn("99887766", stored)
        self.assertNotIn("11223344", stored)

    def test_missing_encryption_key_fails_closed(self) -> None:
        with self.assertRaises(RuntimeError):
            Store(str(Path(self.tmp.name) / "missing.sqlite3"), "")


if __name__ == "__main__":
    unittest.main()
