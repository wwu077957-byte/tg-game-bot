from __future__ import annotations

import re
from dataclasses import dataclass

import phonenumbers
import pycountry
from babel import Locale
from phonenumbers import PhoneNumberFormat


@dataclass(frozen=True)
class Country:
    region: str
    name: str
    english_name: str
    calling_code: str


_ZH_TERRITORIES = Locale.parse("zh_CN").territories
_EN_TERRITORIES = Locale.parse("en").territories
_REGION_TO_CALLING_CODE = {
    region: calling_code
    for calling_code, regions in phonenumbers.COUNTRY_CODE_TO_REGION_CODE.items()
    for region in regions
    if region != "001"
}
_PREFERRED_ORDER = {region: index for index, region in enumerate(("US", "CA", "CN", "GB"))}


def _build_catalog() -> tuple[Country, ...]:
    result: list[Country] = []
    for region, calling_code in _REGION_TO_CALLING_CODE.items():
        item = pycountry.countries.get(alpha_2=region)
        english = item.name if item else str(_EN_TERRITORIES.get(region) or region)
        localized = _ZH_TERRITORIES.get(region) or english
        result.append(Country(region, str(localized), english, f"+{calling_code}"))
    return tuple(
        sorted(
            result,
            key=lambda country: (
                _PREFERRED_ORDER.get(country.region, 99),
                country.name.casefold(),
            ),
        )
    )


COUNTRIES = _build_catalog()
_COUNTRY_BY_REGION = {country.region: country for country in COUNTRIES}


def get_country(region: str) -> Country | None:
    return _COUNTRY_BY_REGION.get(region.upper())


def all_countries() -> tuple[Country, ...]:
    return COUNTRIES


def search_countries(query: str) -> list[Country]:
    needle = query.strip().casefold()
    if not needle:
        return []
    digits = re.sub(r"\D", "", needle)
    if digits and re.fullmatch(r"\+?\d{1,3}", needle):
        calling_code = f"+{digits}"
        return [country for country in COUNTRIES if country.calling_code == calling_code]

    matches: list[Country] = []
    for country in COUNTRIES:
        searchable = [country.region, country.name, country.english_name]
        item = pycountry.countries.get(alpha_2=country.region)
        if item:
            searchable.extend(
                str(value)
                for value in (
                    getattr(item, "official_name", None),
                    getattr(item, "common_name", None),
                )
                if value
            )
        if any(needle in value.casefold() for value in searchable):
            matches.append(country)
    return matches


class PhoneInputError(ValueError):
    pass


def normalize_phone_for_country(value: str, region: str) -> str:
    """Return an E.164 number and reject numbers that conflict with the chosen region."""
    country = get_country(region)
    if not country:
        raise PhoneInputError("不支持所选国家/地区，请重新选择。")
    raw = value.strip()
    if not raw or not re.fullmatch(r"\+?[0-9\s().-]+", raw):
        raise PhoneInputError("号码中只能包含数字、空格、括号、点、横线，或开头的 +。")
    compact = re.sub(r"[\s().-]", "", raw)
    try:
        number = phonenumbers.parse(compact, region)
    except phonenumbers.NumberParseException as exc:
        raise PhoneInputError("无法按所选国家/地区解析这个号码。请核对号码或重新选择国家。") from exc
    if number.extension:
        raise PhoneInputError("请发送手机号本身，不要包含分机号。")
    if not phonenumbers.is_possible_number(number):
        raise PhoneInputError("号码长度或结构不符合所选国家/地区。请核对号码，或重新选择国家。")
    if not phonenumbers.is_valid_number(number):
        raise PhoneInputError("号码虽可解析，但不在所选国家/地区已识别的有效号码范围内。请核对号码。")

    actual_region = phonenumbers.region_code_for_number(number)
    if actual_region and actual_region != "001" and actual_region != country.region:
        actual_country = get_country(actual_region)
        if actual_country:
            raise PhoneInputError(
                f"号码区号/号段更像是{actual_country.name}（{actual_country.calling_code}），"
                f"而不是所选的{country.name}（{country.calling_code}）。请核对号码或重新选择国家。"
            )
    return phonenumbers.format_number(number, PhoneNumberFormat.E164)
