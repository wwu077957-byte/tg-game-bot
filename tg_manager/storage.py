from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken

from .auto_reply import default_config, normalize_config


AUTH_FAILURE_LIMIT = 5
AUTH_PASSWORD_FAILURE_LIMIT = 3
AUTH_FAILURE_WINDOW_SECONDS = 15 * 60
AUTH_LOCKOUT_SECONDS = 5 * 60
AUTH_MIN_INTERVAL_SECONDS = 5
PHONE_CODE_REQUEST_LIMIT = 5
PHONE_CODE_REQUEST_WINDOW_SECONDS = 15 * 60
PHONE_CODE_MIN_INTERVAL_SECONDS = 45


class Store:
    """SQLite store; session blobs and identifying account metadata are Fernet-encrypted."""

    METADATA_SCHEMA_VERSION = 3

    def __init__(self, db_path: str, encryption_key: str) -> None:
        if not encryption_key:
            raise RuntimeError("SESSION_ENCRYPTION_KEY is required; refusing plaintext session storage")
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
            self._scope_key = encryption_key.encode("ascii")
        except Exception as exc:
            raise RuntimeError("SESSION_ENCRYPTION_KEY must be a valid Fernet key") from exc
        os.umask(0o077)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.db_path.parent, 0o700)
        except OSError:
            pass
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _encrypt_text(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def _decrypt_text(self, value: bytes | str | None) -> str:
        if value is None:
            return ""
        try:
            token = value if isinstance(value, bytes) else value.encode("ascii")
            return self._fernet.decrypt(token).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise RuntimeError("Stored metadata could not be decrypted; check the configured encryption key") from exc

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
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
                CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner_id, deleted_at);
                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(owner_id, name)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    owner_id INTEGER PRIMARY KEY,
                    security_alerts INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    account_id INTEGER,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_owner ON audit(owner_id, id DESC);
                CREATE TABLE IF NOT EXISTS auth_attempts (
                    owner_id INTEGER NOT NULL,
                    purpose TEXT NOT NULL,
                    failures INTEGER NOT NULL DEFAULT 0,
                    window_started_at INTEGER NOT NULL,
                    cooldown_until INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_id, purpose)
                );
                CREATE TABLE IF NOT EXISTS auth_attempts_scoped (
                    owner_id INTEGER NOT NULL,
                    purpose TEXT NOT NULL,
                    phone_key TEXT NOT NULL,
                    failures INTEGER NOT NULL DEFAULT 0,
                    window_started_at INTEGER NOT NULL,
                    cooldown_until INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_id, purpose, phone_key)
                );
                CREATE TABLE IF NOT EXISTS phone_code_limits (
                    owner_id INTEGER NOT NULL,
                    phone_key TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    window_started_at INTEGER NOT NULL,
                    next_attempt_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_id, phone_key)
                );
                CREATE TABLE IF NOT EXISTS auto_reply_settings (
                    account_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    manual_auto INTEGER NOT NULL DEFAULT 0,
                    last_human_at REAL NOT NULL DEFAULT 0,
                    paused_until REAL NOT NULL DEFAULT 0,
                    config_cipher BLOB NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS auto_reply_contacts (
                    account_id INTEGER NOT NULL,
                    peer_id INTEGER NOT NULL,
                    first_seen_at REAL NOT NULL,
                    last_inbound_at REAL NOT NULL,
                    pending_count INTEGER NOT NULL DEFAULT 0,
                    last_auto_reply_at REAL NOT NULL DEFAULT 0,
                    daily_day TEXT NOT NULL DEFAULT '',
                    daily_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(account_id, peer_id),
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS auto_reply_messages (
                    account_id INTEGER NOT NULL,
                    peer_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    direction TEXT NOT NULL,
                    auto INTEGER NOT NULL DEFAULT 0,
                    scene TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    PRIMARY KEY(account_id, peer_id, message_id),
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_auto_reply_messages_account_time
                    ON auto_reply_messages(account_id, created_at DESC);
                """
            )
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version < 2:
                self._encrypt_existing_metadata(conn)
                conn.execute("PRAGMA user_version = 2")
                conn.commit()
                # Rewrite old plaintext pages left by the metadata conversion before startup.
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
            if version < self.METADATA_SCHEMA_VERSION:
                # Version 3 adds new tables only; do not encrypt v2 metadata again.
                conn.execute(f"PRAGMA user_version = {self.METADATA_SCHEMA_VERSION}")
                conn.commit()
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass

    def _encrypt_existing_metadata(self, conn: sqlite3.Connection) -> None:
        for row in conn.execute("SELECT id, phone, label FROM accounts").fetchall():
            conn.execute(
                "UPDATE accounts SET phone = ?, label = ? WHERE id = ?",
                (self._encrypt_text(str(row["phone"])), self._encrypt_text(str(row["label"] or "")), row["id"]),
            )
        for row in conn.execute("SELECT id, name FROM groups").fetchall():
            conn.execute(
                "UPDATE groups SET name = ? WHERE id = ?",
                (self._encrypt_text(str(row["name"])), row["id"]),
            )
        for row in conn.execute("SELECT id, action, detail FROM audit").fetchall():
            conn.execute(
                "UPDATE audit SET action = ?, detail = ? WHERE id = ?",
                (
                    self._encrypt_text(str(row["action"])),
                    self._encrypt_text(str(row["detail"] or "")),
                    row["id"],
                ),
            )

    @staticmethod
    def mask_phone(phone: str) -> str:
        if len(phone) <= 5:
            return "*" * len(phone)
        return f"{phone[:3]}{'*' * max(2, len(phone) - 5)}{phone[-2:]}"

    def add_account(self, owner_id: int, phone: str, label: str, session: str) -> int:
        phone_cipher = self._encrypt_text(phone)
        label_cipher = self._encrypt_text(label[:80])
        session_cipher = self._encrypt_text(session)
        now = int(time.time())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT phone FROM accounts WHERE owner_id = ?", (owner_id,)).fetchall()
            if any(self._decrypt_text(row["phone"]) == phone for row in rows):
                raise ValueError("Account already exists for this owner")
            cur = conn.execute(
                "INSERT INTO accounts(owner_id, phone, label, session_cipher, created_at) VALUES(?,?,?,?,?)",
                (owner_id, phone_cipher, label_cipher, session_cipher, now),
            )
            account_id = int(cur.lastrowid)
        self.audit(owner_id, "account_added", account_id, "account added")
        return account_id

    def list_accounts(self, owner_id: int, include_deleted: bool = False) -> list[dict[str, Any]]:
        where = "a.owner_id = ?" if include_deleted else "a.owner_id = ? AND a.deleted_at IS NULL"
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT a.id, a.owner_id, a.phone, a.label, a.group_id, a.created_at,
                           a.deleted_at, g.name AS group_name
                    FROM accounts a LEFT JOIN groups g ON g.id = a.group_id
                    WHERE {where} ORDER BY a.id""",
                (owner_id,),
            ).fetchall()
        result = []
        for item in rows:
            row = dict(item)
            row["phone"] = self._decrypt_text(row["phone"])
            row["label"] = self._decrypt_text(row["label"])
            row["group_name"] = self._decrypt_text(row["group_name"]) if row["group_name"] is not None else None
            result.append(row)
        return result

    def active_owner_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT owner_id FROM accounts WHERE deleted_at IS NULL").fetchall()
        return [int(row[0]) for row in rows]

    def get_account(self, owner_id: int, account_id: int, include_deleted: bool = False) -> dict[str, Any] | None:
        deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, owner_id, phone, label, group_id, created_at, deleted_at FROM accounts "
                "WHERE owner_id = ? AND id = ?" + deleted_clause,
                (owner_id, account_id),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["phone"] = self._decrypt_text(result["phone"])
        result["label"] = self._decrypt_text(result["label"])
        return result

    def get_session(self, owner_id: int, account_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT session_cipher FROM accounts WHERE owner_id = ? AND id = ? AND deleted_at IS NULL",
                (owner_id, account_id),
            ).fetchone()
        if not row:
            return None
        try:
            return self._fernet.decrypt(bytes(row["session_cipher"])).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise RuntimeError("Stored session could not be decrypted; check the configured encryption key") from exc

    def _ensure_auto_reply_row(self, conn: sqlite3.Connection, account_id: int, now: float) -> None:
        row = conn.execute("SELECT 1 FROM auto_reply_settings WHERE account_id = ?", (account_id,)).fetchone()
        if not row:
            payload = self._encrypt_text(json.dumps(default_config(), ensure_ascii=False))
            conn.execute(
                "INSERT INTO auto_reply_settings(account_id, config_cipher, last_human_at) VALUES(?,?,?)",
                (account_id, payload, now),
            )

    def get_auto_reply_settings(self, owner_id: int, account_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT a.id, s.enabled, s.manual_auto, s.last_human_at, s.paused_until, s.config_cipher "
                "FROM accounts a LEFT JOIN auto_reply_settings s ON s.account_id = a.id "
                "WHERE a.owner_id = ? AND a.id = ? AND a.deleted_at IS NULL",
                (owner_id, account_id),
            ).fetchone()
            if not row:
                return None
            saved = self._decrypt_text(row["config_cipher"]) if row["config_cipher"] is not None else "{}"
        try:
            config = normalize_config(json.loads(saved))
        except (TypeError, ValueError):
            config = default_config()
        config.update(
            {
                "account_id": int(row["id"]),
                "enabled": bool(row["enabled"]),
                "manual_auto": bool(row["manual_auto"]),
                "last_human_at": float(row["last_human_at"] or 0),
                "paused_until": float(row["paused_until"] or 0),
            }
        )
        config["pending_count"] = self.auto_reply_pending_count(owner_id, account_id)
        return config

    def save_auto_reply_config(self, owner_id: int, account_id: int, config: dict[str, Any]) -> bool:
        normalized = normalize_config(config)
        payload = self._encrypt_text(json.dumps(normalized, ensure_ascii=False))
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = conn.execute(
                "SELECT 1 FROM accounts WHERE owner_id = ? AND id = ? AND deleted_at IS NULL",
                (owner_id, account_id),
            ).fetchone()
            if not account:
                return False
            self._ensure_auto_reply_row(conn, account_id, now)
            conn.execute("UPDATE auto_reply_settings SET config_cipher = ? WHERE account_id = ?", (payload, account_id))
        return True

    def set_auto_reply_enabled(
        self,
        owner_id: int,
        account_id: int,
        enabled: bool,
        *,
        manual_auto: bool | None = None,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = conn.execute(
                "SELECT 1 FROM accounts WHERE owner_id = ? AND id = ? AND deleted_at IS NULL",
                (owner_id, account_id),
            ).fetchone()
            if not account:
                return False
            self._ensure_auto_reply_row(conn, account_id, current)
            if manual_auto is None:
                conn.execute(
                    "UPDATE auto_reply_settings SET enabled = ?, manual_auto = CASE WHEN ? = 0 THEN 0 ELSE manual_auto END, "
                    "last_human_at = CASE WHEN ? = 1 THEN ? ELSE last_human_at END, "
                    "paused_until = 0 WHERE account_id = ?",
                    (int(enabled), int(enabled), int(enabled), current, account_id),
                )
            else:
                conn.execute(
                    "UPDATE auto_reply_settings SET enabled = ?, manual_auto = ?, "
                    "last_human_at = CASE WHEN ? = 1 THEN ? ELSE last_human_at END, "
                    "paused_until = 0 WHERE account_id = ?",
                    (int(enabled), int(manual_auto), int(enabled), current, account_id),
                )
        return True

    def set_auto_reply_all(self, owner_id: int, enabled: bool, *, now: float | None = None) -> int:
        """Turn automatic replies on in manual-auto mode, or disable them for all active accounts."""
        current = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account_ids = [int(row[0]) for row in conn.execute(
                "SELECT id FROM accounts WHERE owner_id = ? AND deleted_at IS NULL", (owner_id,)
            ).fetchall()]
            for account_id in account_ids:
                self._ensure_auto_reply_row(conn, account_id, current)
                if enabled:
                    conn.execute(
                        "UPDATE auto_reply_settings SET enabled = 1, manual_auto = 1, paused_until = 0, "
                        "last_human_at = CASE WHEN last_human_at = 0 THEN ? ELSE last_human_at END "
                        "WHERE account_id = ?",
                        (current, account_id),
                    )
                else:
                    conn.execute(
                        "UPDATE auto_reply_settings SET enabled = 0, manual_auto = 0, paused_until = 0 "
                        "WHERE account_id = ?",
                        (account_id,),
                    )
        return len(account_ids)

    def active_auto_reply_account_ids(self, owner_id: int) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.id FROM accounts a JOIN auto_reply_settings s ON s.account_id = a.id "
                "WHERE a.owner_id = ? AND a.deleted_at IS NULL AND s.enabled = 1 ORDER BY a.id",
                (owner_id,),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def record_auto_reply_incoming(
        self, owner_id: int, account_id: int, peer_id: int, message_id: int, *, now: float | None = None
    ) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT s.enabled FROM accounts a JOIN auto_reply_settings s ON s.account_id = a.id "
                "WHERE a.owner_id = ? AND a.id = ? AND a.deleted_at IS NULL",
                (owner_id, account_id),
            ).fetchone()
            if not row or not row["enabled"]:
                return {"accepted": False, "first_contact": False, "pending_count": 0}
            cur = conn.execute(
                "INSERT OR IGNORE INTO auto_reply_messages(account_id, peer_id, message_id, direction, auto, created_at) "
                "VALUES(?,?,?,'in',0,?)",
                (account_id, peer_id, message_id, current),
            )
            contact = conn.execute(
                "SELECT pending_count FROM auto_reply_contacts WHERE account_id = ? AND peer_id = ?",
                (account_id, peer_id),
            ).fetchone()
            if cur.rowcount == 0:
                total = int(conn.execute(
                    "SELECT COALESCE(SUM(pending_count),0) FROM auto_reply_contacts WHERE account_id = ?",
                    (account_id,),
                ).fetchone()[0])
                return {"accepted": False, "first_contact": False, "pending_count": total}
            first_contact = contact is None
            if first_contact:
                conn.execute(
                    "INSERT INTO auto_reply_contacts(account_id, peer_id, first_seen_at, last_inbound_at, pending_count) "
                    "VALUES(?,?,?,?,1)",
                    (account_id, peer_id, current, current),
                )
            else:
                conn.execute(
                    "UPDATE auto_reply_contacts SET last_inbound_at = ?, pending_count = pending_count + 1 "
                    "WHERE account_id = ? AND peer_id = ?",
                    (current, account_id, peer_id),
                )
            total = int(conn.execute(
                "SELECT COALESCE(SUM(pending_count),0) FROM auto_reply_contacts WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0])
            return {"accepted": True, "first_contact": first_contact, "pending_count": total}

    def record_auto_reply_human_outgoing(
        self, owner_id: int, account_id: int, peer_id: int, message_id: int, *, now: float | None = None
    ) -> bool:
        current = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT s.config_cipher, s.enabled FROM accounts a JOIN auto_reply_settings s ON s.account_id = a.id "
                "WHERE a.owner_id = ? AND a.id = ? AND a.deleted_at IS NULL",
                (owner_id, account_id),
            ).fetchone()
            if not row or not row["enabled"]:
                return False
            config = normalize_config(json.loads(self._decrypt_text(row["config_cipher"])))
            pause_until = current + int(config["idle_minutes"]) * 60
            inserted = conn.execute(
                "INSERT OR IGNORE INTO auto_reply_messages(account_id, peer_id, message_id, direction, auto, created_at) "
                "VALUES(?,?,?,'out',0,?)",
                (account_id, peer_id, message_id, current),
            )
            if inserted.rowcount == 0:
                return False
            conn.execute(
                "UPDATE auto_reply_settings SET last_human_at = ?, paused_until = ?, manual_auto = 0 WHERE account_id = ?",
                (current, pause_until, account_id),
            )
            conn.execute(
                "UPDATE auto_reply_contacts SET pending_count = 0 WHERE account_id = ? AND peer_id = ?",
                (account_id, peer_id),
            )
        return True

    def auto_reply_pending_count(self, owner_id: int, account_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(c.pending_count),0) FROM auto_reply_contacts c "
                "JOIN accounts a ON a.id = c.account_id "
                "WHERE a.owner_id = ? AND a.id = ? AND a.deleted_at IS NULL",
                (owner_id, account_id),
            ).fetchone()
        return int(row[0]) if row else 0

    def claim_auto_reply(
        self,
        owner_id: int,
        account_id: int,
        peer_id: int,
        *,
        cooldown_seconds: int,
        daily_cap: int,
        local_day: str,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT s.enabled, s.paused_until, c.last_auto_reply_at, c.daily_day, c.daily_count "
                "FROM accounts a JOIN auto_reply_settings s ON s.account_id = a.id "
                "LEFT JOIN auto_reply_contacts c ON c.account_id = a.id AND c.peer_id = ? "
                "WHERE a.owner_id = ? AND a.id = ? AND a.deleted_at IS NULL",
                (peer_id, owner_id, account_id),
            ).fetchone()
            if not row or not row["enabled"] or float(row["paused_until"] or 0) > current:
                return False
            if current - float(row["last_auto_reply_at"] or 0) < max(60, int(cooldown_seconds)):
                if float(row["last_auto_reply_at"] or 0) > 0:
                    return False
            today_count = int(row["daily_count"] or 0) if row["daily_day"] == local_day else 0
            if today_count >= max(1, int(daily_cap)):
                return False
            conn.execute(
                "UPDATE auto_reply_contacts SET last_auto_reply_at = ?, daily_day = ?, daily_count = ? "
                "WHERE account_id = ? AND peer_id = ?",
                (current, local_day, today_count + 1, account_id, peer_id),
            )
        return True

    def release_auto_reply_claim(
        self, account_id: int, peer_id: int, *, claimed_at: float, local_day: str
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE auto_reply_contacts SET last_auto_reply_at = 0, daily_count = CASE "
                "WHEN daily_day = ? AND daily_count > 0 THEN daily_count - 1 ELSE daily_count END "
                "WHERE account_id = ? AND peer_id = ? AND last_auto_reply_at = ?",
                (local_day, account_id, peer_id, claimed_at),
            )

    def record_auto_reply_sent(
        self,
        owner_id: int,
        account_id: int,
        peer_id: int,
        message_id: int,
        scene: str,
        *,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = conn.execute(
                "SELECT 1 FROM accounts WHERE owner_id = ? AND id = ? AND deleted_at IS NULL",
                (owner_id, account_id),
            ).fetchone()
            if not account:
                return False
            conn.execute(
                "INSERT OR REPLACE INTO auto_reply_messages(account_id, peer_id, message_id, direction, auto, scene, created_at) "
                "VALUES(?,?,?,'out',1,?,?)",
                (account_id, peer_id, message_id, scene, current),
            )
            conn.execute(
                "UPDATE auto_reply_contacts SET pending_count = 0 WHERE account_id = ? AND peer_id = ?",
                (account_id, peer_id),
            )
        return True

    def is_auto_reply_message(self, owner_id: int, account_id: int, peer_id: int, message_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM auto_reply_messages m JOIN accounts a ON a.id = m.account_id "
                "WHERE a.owner_id = ? AND a.id = ? AND a.deleted_at IS NULL AND m.peer_id = ? "
                "AND m.message_id = ? AND m.direction = 'out' AND m.auto = 1 LIMIT 1",
                (owner_id, account_id, peer_id, message_id),
            ).fetchone()
        return bool(row)

    def list_auto_reply_messages(self, owner_id: int, account_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m.peer_id, m.message_id, m.scene, m.created_at FROM auto_reply_messages m "
                "JOIN accounts a ON a.id = m.account_id WHERE a.owner_id = ? AND a.id = ? "
                "AND a.deleted_at IS NULL AND m.direction = 'out' AND m.auto = 1 "
                "ORDER BY m.created_at DESC LIMIT ?",
                (owner_id, account_id, max(1, min(limit, 50))),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_label(self, owner_id: int, account_id: int, label: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE accounts SET label = ? WHERE owner_id = ? AND id = ? AND deleted_at IS NULL",
                (self._encrypt_text(label[:80]), owner_id, account_id),
            )
        if cur.rowcount:
            self.audit(owner_id, "account_renamed", account_id, "label updated")
        return bool(cur.rowcount)

    def delete_account(self, owner_id: int, account_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE accounts SET deleted_at = ? WHERE owner_id = ? AND id = ? AND deleted_at IS NULL",
                (int(time.time()), owner_id, account_id),
            )
        if cur.rowcount:
            self.audit(owner_id, "account_trashed", account_id, "soft delete")
        return bool(cur.rowcount)

    def restore_account(self, owner_id: int, account_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE accounts SET deleted_at = NULL WHERE owner_id = ? AND id = ? AND deleted_at IS NOT NULL",
                (owner_id, account_id),
            )
        if cur.rowcount:
            self.audit(owner_id, "account_restored", account_id, "restored from trash")
        return bool(cur.rowcount)

    def create_group(self, owner_id: int, name: str) -> bool:
        name = name.strip()[:60]
        if not name:
            return False
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT name FROM groups WHERE owner_id = ?", (owner_id,)).fetchall()
            if any(self._decrypt_text(row["name"]) == name for row in rows):
                return False
            conn.execute(
                "INSERT INTO groups(owner_id, name, created_at) VALUES(?,?,?)",
                (owner_id, self._encrypt_text(name), int(time.time())),
            )
        self.audit(owner_id, "group_created", None, "group created")
        return True

    def list_groups(self, owner_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name FROM groups WHERE owner_id = ? ORDER BY id",
                (owner_id,),
            ).fetchall()
        result = [{"id": row["id"], "name": self._decrypt_text(row["name"])} for row in rows]
        return sorted(result, key=lambda group: group["name"].casefold())

    def move_to_group(self, owner_id: int, account_id: int, group_name: str) -> bool:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, name FROM groups WHERE owner_id = ?", (owner_id,)).fetchall()
            group = next((r for r in rows if self._decrypt_text(r["name"]) == group_name.strip()), None)
            if not group:
                return False
            cur = conn.execute(
                "UPDATE accounts SET group_id = ? WHERE owner_id = ? AND id = ? AND deleted_at IS NULL",
                (group["id"], owner_id, account_id),
            )
        if cur.rowcount:
            self.audit(owner_id, "account_group_changed", account_id, "group updated")
        return bool(cur.rowcount)

    def set_alerts(self, owner_id: int, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings(owner_id, security_alerts) VALUES(?,?) "
                "ON CONFLICT(owner_id) DO UPDATE SET security_alerts = excluded.security_alerts",
                (owner_id, int(enabled)),
            )
        self.audit(owner_id, "security_alerts_changed", None, "enabled" if enabled else "disabled")

    def alerts_enabled(self, owner_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT security_alerts FROM settings WHERE owner_id = ?", (owner_id,)).fetchone()
        return bool(row[0]) if row else True

    def audit(self, owner_id: int, action: str, account_id: int | None = None, detail: str = "") -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit(owner_id, action, account_id, detail, created_at) VALUES(?,?,?,?,?)",
                (owner_id, self._encrypt_text(action[:64]), account_id, self._encrypt_text(detail[:120]), now),
            )
            # Keep a bounded history; individual event labels and details are encrypted as well.
            conn.execute(
                "DELETE FROM audit WHERE owner_id = ? AND id NOT IN "
                "(SELECT id FROM audit WHERE owner_id = ? ORDER BY id DESC LIMIT 500)",
                (owner_id, owner_id),
            )

    def list_audit(self, owner_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT action, account_id, detail, created_at FROM audit WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
                (owner_id, max(1, min(limit, 50))),
            ).fetchall()
        result = []
        for item in rows:
            row = dict(item)
            row["action"] = self._decrypt_text(row["action"])
            row["detail"] = self._decrypt_text(row["detail"])
            result.append(row)
        return result

    def begin_auth_attempt(
        self, owner_id: int, purpose: str, now: int | None = None, account_phone: str | None = None
    ) -> int:
        """Reserve an attempt if allowed; return seconds remaining in cooldown otherwise."""
        if purpose not in {"code", "password"}:
            raise ValueError("Unsupported authentication purpose")
        current = int(time.time()) if now is None else int(now)
        if account_phone is not None:
            return self._begin_scoped_auth_attempt(owner_id, purpose, account_phone, current)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT failures, window_started_at, cooldown_until, next_attempt_at "
                "FROM auth_attempts WHERE owner_id = ? AND purpose = ?",
                (owner_id, purpose),
            ).fetchone()
            if row and row["cooldown_until"] > current:
                return max(1, int(row["cooldown_until"] - current))
            expired = not row or current - row["window_started_at"] >= AUTH_FAILURE_WINDOW_SECONDS
            if row and row["next_attempt_at"] > current and not expired:
                return max(1, int(row["next_attempt_at"] - current))
            failures = 0 if expired else int(row["failures"])
            window_started = current if expired else int(row["window_started_at"])
            conn.execute(
                "INSERT INTO auth_attempts(owner_id, purpose, failures, window_started_at, cooldown_until, next_attempt_at) "
                "VALUES(?,?,?,?,0,?) ON CONFLICT(owner_id, purpose) DO UPDATE SET "
                "failures = excluded.failures, window_started_at = excluded.window_started_at, "
                "cooldown_until = 0, next_attempt_at = excluded.next_attempt_at",
                (owner_id, purpose, failures, window_started, current + AUTH_MIN_INTERVAL_SECONDS),
            )
            conn.execute(
                "DELETE FROM auth_attempts WHERE window_started_at < ? AND cooldown_until < ?",
                (current - 24 * 60 * 60, current),
            )
        return 0

    def record_auth_failure(
        self, owner_id: int, purpose: str, now: int | None = None, account_phone: str | None = None
    ) -> int:
        """Count a rejected code/password; lock that purpose after the failure limit."""
        if purpose not in {"code", "password"}:
            raise ValueError("Unsupported authentication purpose")
        current = int(time.time()) if now is None else int(now)
        if account_phone is not None:
            return self._record_scoped_auth_failure(owner_id, purpose, account_phone, current)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT failures, window_started_at, next_attempt_at FROM auth_attempts "
                "WHERE owner_id = ? AND purpose = ?",
                (owner_id, purpose),
            ).fetchone()
            expired = not row or current - row["window_started_at"] >= AUTH_FAILURE_WINDOW_SECONDS
            failures = (0 if expired else int(row["failures"])) + 1
            window_started = current if expired else int(row["window_started_at"])
            failure_limit = AUTH_PASSWORD_FAILURE_LIMIT if purpose == "password" else AUTH_FAILURE_LIMIT
            cooldown_until = current + AUTH_LOCKOUT_SECONDS if failures >= failure_limit else 0
            if cooldown_until:
                failures = 0
                window_started = current
            conn.execute(
                "INSERT INTO auth_attempts(owner_id, purpose, failures, window_started_at, cooldown_until, next_attempt_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id, purpose) DO UPDATE SET "
                "failures = excluded.failures, window_started_at = excluded.window_started_at, "
                "cooldown_until = excluded.cooldown_until, next_attempt_at = excluded.next_attempt_at",
                (
                    owner_id,
                    purpose,
                    failures,
                    window_started,
                    cooldown_until,
                    max(current + AUTH_MIN_INTERVAL_SECONDS, int(row["next_attempt_at"]) if row else 0),
                ),
            )
        return max(0, cooldown_until - current)

    def _phone_scope_key(self, account_phone: str) -> str:
        value = account_phone.strip()
        if not value:
            raise ValueError("Authentication scope must not be empty")
        message = b"tgvault-auth-scope-v1:" + value.encode("utf-8")
        return hmac.new(self._scope_key, message, hashlib.sha256).hexdigest()

    def _begin_scoped_auth_attempt(self, owner_id: int, purpose: str, account_phone: str, current: int) -> int:
        phone_key = self._phone_scope_key(account_phone)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            legacy = conn.execute(
                "SELECT window_started_at, cooldown_until, next_attempt_at FROM auth_attempts "
                "WHERE owner_id = ? AND purpose = ?",
                (owner_id, purpose),
            ).fetchone()
            if legacy and legacy["cooldown_until"] > current:
                return max(1, int(legacy["cooldown_until"] - current))
            if (
                legacy
                and current - int(legacy["window_started_at"]) < AUTH_FAILURE_WINDOW_SECONDS
                and legacy["next_attempt_at"] > current
            ):
                return max(1, int(legacy["next_attempt_at"] - current))
            row = conn.execute(
                "SELECT failures, window_started_at, cooldown_until, next_attempt_at "
                "FROM auth_attempts_scoped WHERE owner_id = ? AND purpose = ? AND phone_key = ?",
                (owner_id, purpose, phone_key),
            ).fetchone()
            if row and row["cooldown_until"] > current:
                return max(1, int(row["cooldown_until"] - current))
            expired = not row or current - row["window_started_at"] >= AUTH_FAILURE_WINDOW_SECONDS
            if row and row["next_attempt_at"] > current and not expired:
                return max(1, int(row["next_attempt_at"] - current))
            failures = 0 if expired else int(row["failures"])
            window_started = current if expired else int(row["window_started_at"])
            conn.execute(
                "INSERT INTO auth_attempts_scoped(owner_id, purpose, phone_key, failures, window_started_at, cooldown_until, next_attempt_at) "
                "VALUES(?,?,?,?,?,0,?) ON CONFLICT(owner_id, purpose, phone_key) DO UPDATE SET "
                "failures = excluded.failures, window_started_at = excluded.window_started_at, "
                "cooldown_until = 0, next_attempt_at = excluded.next_attempt_at",
                (owner_id, purpose, phone_key, failures, window_started, current + AUTH_MIN_INTERVAL_SECONDS),
            )
            conn.execute(
                "DELETE FROM auth_attempts_scoped WHERE window_started_at < ? AND cooldown_until < ?",
                (current - 24 * 60 * 60, current),
            )
        return 0

    def _record_scoped_auth_failure(self, owner_id: int, purpose: str, account_phone: str, current: int) -> int:
        phone_key = self._phone_scope_key(account_phone)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT failures, window_started_at, next_attempt_at FROM auth_attempts_scoped "
                "WHERE owner_id = ? AND purpose = ? AND phone_key = ?",
                (owner_id, purpose, phone_key),
            ).fetchone()
            expired = not row or current - row["window_started_at"] >= AUTH_FAILURE_WINDOW_SECONDS
            failures = (0 if expired else int(row["failures"])) + 1
            window_started = current if expired else int(row["window_started_at"])
            failure_limit = AUTH_PASSWORD_FAILURE_LIMIT if purpose == "password" else AUTH_FAILURE_LIMIT
            cooldown_until = current + AUTH_LOCKOUT_SECONDS if failures >= failure_limit else 0
            if cooldown_until:
                failures = 0
                window_started = current
            conn.execute(
                "INSERT INTO auth_attempts_scoped(owner_id, purpose, phone_key, failures, window_started_at, cooldown_until, next_attempt_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id, purpose, phone_key) DO UPDATE SET "
                "failures = excluded.failures, window_started_at = excluded.window_started_at, "
                "cooldown_until = excluded.cooldown_until, next_attempt_at = excluded.next_attempt_at",
                (
                    owner_id,
                    purpose,
                    phone_key,
                    failures,
                    window_started,
                    cooldown_until,
                    max(current + AUTH_MIN_INTERVAL_SECONDS, int(row["next_attempt_at"]) if row else 0),
                ),
            )
        return max(0, cooldown_until - current)

    def defer_auth_attempt(
        self,
        owner_id: int,
        purpose: str,
        seconds: int,
        account_phone: str | None = None,
        now: int | None = None,
    ) -> None:
        """Persist Telegram's server-mandated FloodWait for one auth scope."""
        if purpose not in {"code", "password"}:
            raise ValueError("Unsupported authentication purpose")
        current = int(time.time()) if now is None else int(now)
        until = current + max(1, int(seconds))
        scoped_table = "auth_attempts_scoped" if account_phone is not None else "auth_attempts"
        scope_values: tuple[Any, ...] = (owner_id, purpose, self._phone_scope_key(account_phone)) if account_phone is not None else (owner_id, purpose)
        where = "owner_id = ? AND purpose = ? AND phone_key = ?" if account_phone is not None else "owner_id = ? AND purpose = ?"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT failures, window_started_at, cooldown_until, next_attempt_at FROM {scoped_table} WHERE {where}",
                scope_values,
            ).fetchone()
            expired = not row or current - int(row["window_started_at"]) >= AUTH_FAILURE_WINDOW_SECONDS
            failures = 0 if expired else int(row["failures"])
            window_started = current if expired else int(row["window_started_at"])
            cooldown_until = max(until, int(row["cooldown_until"]) if row else 0)
            next_attempt_at = max(cooldown_until, int(row["next_attempt_at"]) if row else 0)
            if account_phone is not None:
                conn.execute(
                    "INSERT INTO auth_attempts_scoped(owner_id, purpose, phone_key, failures, window_started_at, cooldown_until, next_attempt_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id, purpose, phone_key) DO UPDATE SET "
                    "failures=excluded.failures, window_started_at=excluded.window_started_at, "
                    "cooldown_until=excluded.cooldown_until, next_attempt_at=excluded.next_attempt_at",
                    (*scope_values, failures, window_started, cooldown_until, next_attempt_at),
                )
            else:
                conn.execute(
                    "INSERT INTO auth_attempts(owner_id, purpose, failures, window_started_at, cooldown_until, next_attempt_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id, purpose) DO UPDATE SET "
                    "failures=excluded.failures, window_started_at=excluded.window_started_at, "
                    "cooldown_until=excluded.cooldown_until, next_attempt_at=excluded.next_attempt_at",
                    (*scope_values, failures, window_started, cooldown_until, next_attempt_at),
                )

    def reserve_phone_code_request(self, owner_id: int, phone: str, now: int | None = None) -> tuple[int, int]:
        """Reserve one initial/resend request, enforcing a per-owner, per-number window."""
        current = int(time.time()) if now is None else int(now)
        phone_key = self._phone_scope_key(phone)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempts, window_started_at, next_attempt_at FROM phone_code_limits "
                "WHERE owner_id = ? AND phone_key = ?",
                (owner_id, phone_key),
            ).fetchone()
            expired = not row or current - int(row["window_started_at"]) >= PHONE_CODE_REQUEST_WINDOW_SECONDS
            attempts = 0 if expired else int(row["attempts"])
            window_started = current if expired else int(row["window_started_at"])
            next_attempt_at = int(row["next_attempt_at"]) if row else 0
            wait_seconds = max(0, next_attempt_at - current)
            if attempts >= PHONE_CODE_REQUEST_LIMIT:
                wait_seconds = max(wait_seconds, window_started + PHONE_CODE_REQUEST_WINDOW_SECONDS - current)
            if wait_seconds > 0:
                return max(1, int(wait_seconds)), attempts
            attempts += 1
            conn.execute(
                "INSERT INTO phone_code_limits(owner_id, phone_key, attempts, window_started_at, next_attempt_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(owner_id, phone_key) DO UPDATE SET "
                "attempts=excluded.attempts, window_started_at=excluded.window_started_at, "
                "next_attempt_at=excluded.next_attempt_at",
                (owner_id, phone_key, attempts, window_started, current + PHONE_CODE_MIN_INTERVAL_SECONDS),
            )
            conn.execute(
                "DELETE FROM phone_code_limits WHERE window_started_at < ? AND next_attempt_at < ?",
                (current - 24 * 60 * 60, current),
            )
        return 0, attempts

    def defer_phone_code_request(self, owner_id: int, phone: str, seconds: int, now: int | None = None) -> None:
        """Persist Telegram's FloodWait without storing the phone number itself."""
        current = int(time.time()) if now is None else int(now)
        phone_key = self._phone_scope_key(phone)
        until = current + max(1, int(seconds))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempts, window_started_at, next_attempt_at FROM phone_code_limits "
                "WHERE owner_id = ? AND phone_key = ?",
                (owner_id, phone_key),
            ).fetchone()
            expired = not row or current - int(row["window_started_at"]) >= PHONE_CODE_REQUEST_WINDOW_SECONDS
            attempts = 0 if expired else int(row["attempts"])
            window_started = current if expired else int(row["window_started_at"])
            next_attempt_at = max(until, int(row["next_attempt_at"]) if row else 0)
            conn.execute(
                "INSERT INTO phone_code_limits(owner_id, phone_key, attempts, window_started_at, next_attempt_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(owner_id, phone_key) DO UPDATE SET "
                "attempts=excluded.attempts, window_started_at=excluded.window_started_at, "
                "next_attempt_at=excluded.next_attempt_at",
                (owner_id, phone_key, attempts, window_started, next_attempt_at),
            )

    def clear_auth_attempts(
        self, owner_id: int, purpose: str | None = None, account_phone: str | None = None
    ) -> None:
        if account_phone is not None:
            phone_key = self._phone_scope_key(account_phone)
            with self._connect() as conn:
                if purpose is None:
                    conn.execute(
                        "DELETE FROM auth_attempts_scoped WHERE owner_id = ? AND phone_key = ?",
                        (owner_id, phone_key),
                    )
                else:
                    conn.execute(
                        "DELETE FROM auth_attempts_scoped WHERE owner_id = ? AND purpose = ? AND phone_key = ?",
                        (owner_id, purpose, phone_key),
                    )
            return
        with self._connect() as conn:
            if purpose is None:
                conn.execute("DELETE FROM auth_attempts WHERE owner_id = ?", (owner_id,))
            else:
                conn.execute(
                    "DELETE FROM auth_attempts WHERE owner_id = ? AND purpose = ?",
                    (owner_id, purpose),
                )
