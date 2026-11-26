from __future__ import annotations

import unittest

from tg_manager.security import AllowlistConfigurationError, is_allowed_user_id, parse_allowed_user_ids


class AllowlistTests(unittest.TestCase):
    def test_parses_only_positive_numeric_ids(self) -> None:
        self.assertEqual(parse_allowed_user_ids("123, 456,123"), frozenset({123, 456}))

    def test_only_listed_users_are_authorized(self) -> None:
        allowed = parse_allowed_user_ids("123,456")
        self.assertTrue(is_allowed_user_id(123, allowed))
        self.assertFalse(is_allowed_user_id(789, allowed))
        self.assertFalse(is_allowed_user_id(None, allowed))

    def test_missing_or_empty_allowlist_fails_closed(self) -> None:
        for value in (None, "", "   ", ","):
            with self.subTest(value=value), self.assertRaises(AllowlistConfigurationError):
                parse_allowed_user_ids(value)

    def test_invalid_values_are_rejected(self) -> None:
        for value in ("@friend", "+123", "123;456", "0", "-1", "１２３"):
            with self.subTest(value=value), self.assertRaises(AllowlistConfigurationError):
                parse_allowed_user_ids(value)


if __name__ == "__main__":
    unittest.main()
