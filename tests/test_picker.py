from __future__ import annotations

import unittest

from tg_manager.picker import build_account_picker, make_picker_callback, parse_picker_callback


class AccountPickerTests(unittest.TestCase):
    def test_callback_round_trip_for_supported_actions(self) -> None:
        for action in ("code", "devices", "twofa"):
            with self.subTest(action=action):
                callback = make_picker_callback(action, 42)
                self.assertEqual(parse_picker_callback(callback), (action, 42))

    def test_rejects_unknown_or_malformed_callbacks(self) -> None:
        for callback in (None, "", "acctpick:delete:1", "acctpick:devices:0", "acctpick:twofa:-1", "acctpick:code:1:2", "acctpick:code:999999999999999999999999999999999"):
            with self.subTest(callback=callback):
                self.assertIsNone(parse_picker_callback(callback))
        with self.assertRaises(ValueError):
            make_picker_callback("delete", 1)
        with self.assertRaises(ValueError):
            make_picker_callback("code", 0)
        with self.assertRaises(ValueError):
            make_picker_callback("code", 2**63)
        self.assertIsNone(parse_picker_callback(f"acctpick:code:{2**63}"))

    def test_keyboard_uses_only_the_supplied_owner_rows(self) -> None:
        rows = [
            {"id": 3, "phone": "+15551112222", "label": "Personal", "group_name": None},
            {"id": 8, "phone": "+447700900123", "label": "Work", "group_name": None},
        ]
        keyboard = build_account_picker(rows, "devices", lambda row: f"#{row['id']} {row['label']}")
        self.assertEqual(
            [[button.callback_data for button in line] for line in keyboard.inline_keyboard],
            [["acctpick:devices:3"], ["acctpick:devices:8"]],
        )
        self.assertEqual(
            [[button.text for button in line] for line in keyboard.inline_keyboard],
            [["#3 Personal"], ["#8 Work"]],
        )


if __name__ == "__main__":
    unittest.main()
