from __future__ import annotations

import unittest

from tg_manager.countries import (
    PhoneInputError,
    all_countries,
    get_country,
    normalize_phone_for_country,
    search_countries,
)


class CountryPickerTests(unittest.TestCase):
    def test_search_works_for_chinese_english_and_calling_code(self) -> None:
        self.assertEqual(get_country("US").name, "美国")
        self.assertEqual(get_country("CA").name, "加拿大")
        self.assertIn("CN", {country.region for country in search_countries("中国")})
        self.assertIn("US", {country.region for country in search_countries("United States")})
        self.assertIn("CA", {country.region for country in search_countries("+1")})

    def test_local_numbers_are_normalized_to_e164(self) -> None:
        self.assertEqual(normalize_phone_for_country("13800138000", "CN"), "+8613800138000")
        self.assertEqual(normalize_phone_for_country("415-555-2671", "US"), "+14155552671")
        self.assertEqual(normalize_phone_for_country("416 555 0123", "CA"), "+14165550123")

    def test_shared_us_canada_calling_code_is_disambiguated_by_number_plan(self) -> None:
        with self.assertRaisesRegex(PhoneInputError, "加拿大"):
            normalize_phone_for_country("416-555-0123", "US")
        with self.assertRaisesRegex(PhoneInputError, "美国"):
            normalize_phone_for_country("415-555-2671", "CA")

    def test_phone_validation_errors_can_be_localized(self) -> None:
        with self.assertRaises(PhoneInputError) as raised:
            normalize_phone_for_country("416-555-0123", "US")
        self.assertIn("Canada", raised.exception.localized("en"))
        self.assertIn("加拿大", raised.exception.localized("zh"))

    def test_catalog_includes_all_phone_regions_and_rejects_unassigned_numbers(self) -> None:
        regions = {country.region for country in search_countries("+1")}
        self.assertIn("CA", regions)
        all_regions = {country.region for country in all_countries()}
        self.assertTrue({"AC", "TA", "XK"}.issubset(all_regions))
        with self.assertRaisesRegex(PhoneInputError, "有效号码"):
            normalize_phone_for_country("1111111111", "US")

    def test_rejects_impossible_number_and_unsupported_country(self) -> None:
        with self.assertRaises(PhoneInputError):
            normalize_phone_for_country("123", "US")
        with self.assertRaises(PhoneInputError):
            normalize_phone_for_country("1234567890", "ZZ")


if __name__ == "__main__":
    unittest.main()
