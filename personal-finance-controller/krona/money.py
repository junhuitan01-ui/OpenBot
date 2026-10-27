"""Exact money handling. All authoritative amounts are integer cents."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

MAX_CENTS = 1_000_000_000  # $10,000,000.00 - anything larger is rejected as implausible.


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as a valid SGD amount."""


_CURRENCY = re.compile(r"^\s*(?:S\$|SGD|\$)?\s*", re.IGNORECASE)
_TRAILING = re.compile(r"\s*(?:SGD|dollars?|bucks)?\s*$", re.IGNORECASE)
_NUMBER = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$|^\.\d+$")


def to_decimal(value: str | int | Decimal) -> Decimal:
    """Parse a plain or currency-prefixed number into a Decimal (no float path)."""
    if isinstance(value, bool):
        raise MoneyError("not a number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    if not isinstance(value, str):
        raise MoneyError("amount must be text")
    s = _TRAILING.sub("", _CURRENCY.sub("", value.strip()))
    if not _NUMBER.match(s):
        raise MoneyError(f"not an amount: {value!r}")
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise MoneyError(f"not an amount: {value!r}") from exc


def parse_cents(value: str | int | Decimal, *, allow_zero: bool = False) -> int:
    """Convert an amount to integer cents. Rejects more than 2 decimal places."""
    d = to_decimal(value)
    if d.is_signed() or (d == 0 and not allow_zero):
        raise MoneyError("amount must be positive")
    cents = d * 100
    if cents != cents.to_integral_value():
        raise MoneyError("amounts cannot have more than 2 decimal places")
    c = int(cents)
    if c > MAX_CENTS:
        raise MoneyError("amount is implausibly large")
    return c


def fmt(cents: int) -> str:
    """Format cents as $1,234.56 (negative as -$1.00)."""
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def pct(part: int, whole: int) -> int:
    """Whole-number percentage, truncated toward zero, computed with integers."""
    if whole <= 0:
        return 0
    return (part * 100) // whole


_TEXT_NUMBER = re.compile(r"(?<![\d.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|(?<![\w\d])\.\d+")


def numbers_in_text(text: str) -> set[Decimal]:
    """Every numeric value literally present in a message (used to reject invented amounts)."""
    out: set[Decimal] = set()
    for m in _TEXT_NUMBER.finditer(text or ""):
        try:
            out.add(Decimal(m.group(0).replace(",", "")).normalize())
        except InvalidOperation:  # pragma: no cover
            continue
    return out


def cents_to_decimal(cents: int) -> Decimal:
    return (Decimal(cents) / 100).normalize()
