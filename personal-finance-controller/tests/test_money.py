from decimal import Decimal

import pytest

from krona.money import MoneyError, fmt, numbers_in_text, parse_cents, pct


@pytest.mark.parametrize("raw,cents", [
    ("12", 1200), ("$12", 1200), ("S$12", 1200), ("SGD 12", 1200), ("sgd12.50", 1250), ("3.80", 380),
    ("3.8", 380), ("1,234.56", 123456), ("$0.05", 5), ("12 SGD", 1200),
])
def test_parse_cents(raw, cents):
    assert parse_cents(raw) == cents


@pytest.mark.parametrize("raw", ["-5", "0", "1.234", "abc", "", "12.3.4", "1e3", "99999999999"])
def test_parse_cents_rejects(raw):
    with pytest.raises(MoneyError):
        parse_cents(raw)


def test_exact_arithmetic_no_float_drift():
    # 0.1 + 0.2 style errors cannot happen with integer cents.
    assert parse_cents("0.10") + parse_cents("0.20") == parse_cents("0.30")
    assert sum(parse_cents("3.80") for _ in range(1000)) == 380000
    assert parse_cents("3.80") * 2 == 760


def test_fmt_and_pct():
    assert fmt(760) == "$7.60"
    assert fmt(123456789) == "$1,234,567.89"
    assert fmt(-500) == "-$5.00"
    assert pct(8200, 10000) == 82
    assert pct(1, 0) == 0


def test_numbers_in_text():
    assert numbers_in_text("2 milks at $3.80 each, S$1,200 total") == {Decimal(2), Decimal("3.8"), Decimal(1200)}
