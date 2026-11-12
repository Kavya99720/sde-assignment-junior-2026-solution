from datetime import date
from decimal import Decimal

import pytest

from solution.normalise import (
    ist_date, ist_iso, normalise_header, normalise_phone, parse_date, parse_decimal,
    parse_enum, parse_timestamp,
)


@pytest.mark.parametrize("raw", [
    "9876543210", "+91 98765 43210", "09876543210", "91-98765-43210",
    "(+91) 9876543210", "+919876543210", "  98765 43210  ",
])
def test_phone_valid_formats(raw):
    assert normalise_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",            # 9 digits
    "5876543210",           # bad prefix
    "022-2345-6789",        # landline: 0 dropped, then starts with 2
    "+1 415 555 0134",      # 11 digits starting with 1
    "N/A", "", "   ", None,
    "129876543210",         # 12 digits not starting with 91
    "00919876543210",       # 14 digits
    "919876543210 1",       # 13 digits
])
def test_phone_invalid(raw):
    assert normalise_phone(raw) is None


def test_phone_10_digits_starting_91_is_not_stripped():
    assert normalise_phone("9123456789") == "+919123456789"
    assert normalise_phone("91 23456 789") == "+919123456789"


def test_phone_11_digits_starting_0_only():
    assert normalise_phone("09123456789") == "+919123456789"
    assert normalise_phone("01234567890") is None  # 0 dropped -> starts with 1


@pytest.mark.parametrize("raw, expected", [
    ("Mobile No", "mobile no"), ("  MOBILE__no ", "mobile no"), ("policy_number", "policy number"),
    ("Customer \t Name", "customer name"), ("phone", "phone"),
])
def test_header(raw, expected):
    assert normalise_header(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("2026-12-01", date(2026, 12, 1)),
    ("01/12/2026", date(2026, 12, 1)),     # day-first
    ("05/06/2026", date(2026, 6, 5)),
    ("01-12-2026", date(2026, 12, 1)),
    ("01-Dec-2026", date(2026, 12, 1)),
    ("01-DEC-2026", date(2026, 12, 1)),
    ("1-dec-2026", date(2026, 12, 1)),
    ("29/02/2028", date(2028, 2, 29)),
    (" 2026-10-03 ", date(2026, 10, 3)),
])
def test_date_valid(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "31/02/2026", "2026-13-01", "29/02/2026", "TBD", "soon", "", None,
    "13/13/2026", "01-Foo-2026", "2026/12/01", "12/01/26", "0000-01-01",
])
def test_date_invalid(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw, expected", [
    ("12345", "12345.00"), ("12345.5", "12345.50"), ("₹12,345", "12345.00"),
    ("₹1,20,000", "120000.00"),            # lakh grouping
    ("₹ 1,20,000.5", "120000.50"),
    ("Rs. 4000", "4000.00"), ("Rs 4000", "4000.00"), ("rs.4000", "4000.00"), ("Rs4000", "4000.00"),
    ("INR 12000.5", "12000.50"), ("inr12000", "12000.00"),
    ("1200.456", "1200.46"), ("1200.455", "1200.46"),  # half-up, not banker's/float
    ("0", "0.00"), (" 10 ", "10.00"), (".5", "0.50"),
])
def test_decimal_valid(raw, expected):
    assert parse_decimal(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["-5", "₹-5", "N/A", "-", "twelve thousand", "", None, "Rs", "1.2.3", "12abc"])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


def test_enum():
    langs = ["en", "hi", "ta"]
    assert parse_enum("HI", langs) == "hi"
    assert parse_enum(" en ", langs) == "en"
    assert parse_enum("Hindi", langs) is None
    assert parse_enum("", langs) is None
    assert parse_enum(None, langs) is None


def test_timestamp_forms_agree():
    z = parse_timestamp("2026-10-03T04:31:00Z")
    off = parse_timestamp("2026-10-03T10:01:00+05:30")
    naive = parse_timestamp("2026-10-03 10:01:00")      # naive means IST
    assert z == off == naive


def test_timestamp_other_forms():
    base = parse_timestamp("2026-10-03T10:01:00+05:30")
    assert parse_timestamp("2026-10-03T10:01:00") == base           # naive with T
    assert parse_timestamp("2026-10-03T04:31:00+00:00") == base
    assert parse_timestamp("2026-10-03T04:31:00z") == base
    assert parse_timestamp("2026-10-03T10:01:00.750+05:30") == base  # fraction floors


@pytest.mark.parametrize("raw", ["", "not a time", "2026-10-03", "2026-13-03 10:00:00", None, 12345, "2026-10-03T25:00:00Z"])
def test_timestamp_invalid(raw):
    assert parse_timestamp(raw) is None


def test_ist_date_crosses_midnight():
    e = parse_timestamp("2026-10-05T19:00:00Z")  # 00:30 IST on the 6th
    assert ist_date(e) == date(2026, 10, 6)
    e2 = parse_timestamp("2026-10-05T18:29:59Z")  # 23:59:59 IST on the 5th
    assert ist_date(e2) == date(2026, 10, 5)


def test_ist_iso():
    assert ist_iso(parse_timestamp("2026-10-14T12:25:24Z")) == "2026-10-14T17:55:24+05:30"
