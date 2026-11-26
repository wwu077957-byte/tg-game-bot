from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from tg_manager.storage import AUTH_LOCKOUT_SECONDS, Store


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

    def test_wrong_attempts_are_spaced_and_success_can_clear_throttle(self) -> None:
        now = 20_000
        self.assertEqual(self.store.begin_auth_attempt(101, "password", now=now), 0)
        self.store.record_auth_failure(101, "password", now=now)
        self.assertEqual(self.store.begin_auth_attempt(101, "password", now=now + 1), 4)
        self.assertEqual(self.store.begin_auth_attempt(101, "password", now=now + 5), 0)
        self.store.clear_auth_attempts(101, "password")
        self.assertEqual(self.store.begin_auth_attempt(101, "password", now=now + 6), 0)

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

    def test_missing_encryption_key_fails_closed(self) -> None:
        with self.assertRaises(RuntimeError):
            Store(str(Path(self.tmp.name) / "missing.sqlite3"), "")


if __name__ == "__main__":
    unittest.main()
