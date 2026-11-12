"""Pure value normalisers. No I/O, no database. Each parser returns None when the value is invalid."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

IST = timezone(timedelta(hours=5, minutes=30))

_HEADER_SEP = re.compile(r"[_\s]+")
_NON_DIGIT = re.compile(r"[^0-9]")


def normalise_header(raw: str) -> str:
    """Lower-case, trim, then collapse every run of '_' or whitespace to one space."""
    return _HEADER_SEP.sub(" ", raw.lower().strip())


def normalise_phone(raw: object) -> str | None:
    """Indian mobile to canonical '+91XXXXXXXXXX', or None if invalid."""
    if raw is None:
        return None
    digits = _NON_DIGIT.sub("", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_ISO_DATE = re.compile(r"^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})$")
_DMY_SLASH = re.compile(r"^([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})$")
_DMY_DASH = re.compile(r"^([0-9]{1,2})-([0-9]{1,2})-([0-9]{4})$")
_D_MON_Y = re.compile(r"^([0-9]{1,2})-([A-Za-z]{3})-([0-9]{4})$")


def parse_date(raw: object) -> date | None:
    """YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY (day-first). Impossible dates are None."""
    if raw is None:
        return None
    s = str(raw).strip()
    try:
        if m := _ISO_DATE.match(s):
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
        elif (m := _DMY_SLASH.match(s)) or (m := _DMY_DASH.match(s)):
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
        elif m := _D_MON_Y.match(s):
            mo = _MONTHS.get(m[2].lower())
            if mo is None:
                return None
            d, y = int(m[1]), int(m[3])
        else:
            return None
        return date(y, mo, d)
    except ValueError:
        return None


_CURRENCY_PREFIX = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_AMOUNT = re.compile(r"^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$")
_CENTS = Decimal("0.01")


def parse_decimal(raw: object) -> Decimal | None:
    """Optional currency prefix, comma thousands separators (any grouping), >= 0, 2 places half-up."""
    if raw is None:
        return None
    s = _CURRENCY_PREFIX.sub("", str(raw).strip(), count=1).replace(",", "").strip()
    if not _AMOUNT.match(s):
        return None
    return Decimal(s).quantize(_CENTS, rounding=ROUND_HALF_UP)


def parse_enum(raw: object, values: list[str]) -> str | None:
    """Case-insensitive match against `values`; stored lower-case."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    return s if s in {v.lower() for v in values} else None


_TS = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?"
    r"(?:[Zz]|[+-][0-9]{2}:?[0-9]{2})?$"
)


def parse_timestamp(raw: object) -> int | None:
    """Event timestamp to UTC epoch seconds. Naive values are IST. None if unparseable."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not _TS.match(s):
        return None
    if s[-1] in "zZ":
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return int(dt.timestamp() // 1)


def ist_date(epoch: int) -> date:
    return datetime.fromtimestamp(epoch, IST).date()


def ist_iso(epoch: int) -> str:
    """e.g. 2026-10-14T17:55:24+05:30"""
    return datetime.fromtimestamp(epoch, IST).isoformat()
