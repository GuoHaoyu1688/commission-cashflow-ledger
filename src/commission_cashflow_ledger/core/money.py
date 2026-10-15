"""Integer cents and explicit half-up rounding; no binary float arithmetic."""

from __future__ import annotations

import calendar
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

MAX_CENTS = 9_000_000_000_000_000


def decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a monetary or rate value")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("expected a decimal number") from exc
    if not amount.is_finite():
        raise ValueError("number must be finite")
    return amount


def cents(value: Any, allow_negative: bool = False) -> int:
    amount = decimal(value) * 100
    if amount != amount.to_integral_value() or abs(amount) > MAX_CENTS:
        raise ValueError("money must have at most two decimal places and fit the supported range")
    if amount < 0 and not allow_negative:
        raise ValueError("amount cannot be negative")
    return int(amount)


def rounded(value: Decimal) -> int:
    result = int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if abs(result) > MAX_CENTS:
        raise ValueError("calculated amount exceeds supported range")
    return result


def amount(value: int) -> str:
    return format(Decimal(value) / 100, ".2f")


def rate(value: Any) -> Decimal:
    result = decimal(value)
    if not 0 <= result <= 1:
        raise ValueError("rates must be between 0 and 1 (for example 0.03 for 3%)")
    return result


def iso_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def month_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("month must use YYYY-MM")
    return date.fromisoformat(value + "-01")


def add_months(value: date, count: int) -> date:
    index = value.year * 12 + value.month - 1 + count
    year, month0 = divmod(index, 12)
    return date(year, month0 + 1, min(value.day, calendar.monthrange(year, month0 + 1)[1]))
