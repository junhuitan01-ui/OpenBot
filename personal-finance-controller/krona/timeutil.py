"""Singapore-time helpers. KRONA reasons about dates in Asia/Singapore only."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")


@dataclass
class Clock:
    """Injectable clock so tests can pin 'now'. Always returns an aware SGT datetime."""

    source: Callable[[], datetime] | None = None

    def now(self) -> datetime:
        if self.source is None:
            return datetime.now(timezone.utc).astimezone(SGT)
        dt = self.source()
        if dt.tzinfo is None:
            raise ValueError("clock source must return an aware datetime")
        return dt.astimezone(SGT)

    def today(self) -> date:
        return self.now().date()


def fixed_clock(dt: datetime) -> Clock:
    holder = {"dt": dt}
    c = Clock(lambda: holder["dt"])
    c.holder = holder  # type: ignore[attr-defined]
    return c


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def month_bounds(d: date) -> tuple[date, date]:
    last = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=1), d.replace(day=last)


def days_in_month(d: date) -> int:
    return calendar.monthrange(d.year, d.month)[1]


def previous_month(d: date) -> date:
    first = d.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


def period_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def at_sgt(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=SGT)


def short_date(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


def long_date(d: date) -> str:
    return f"{d.strftime('%A')}, {d.day} {d.strftime('%B %Y')}"


_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def parse_date_arg(text: str, today: date) -> date | None:
    """Parse YYYY-MM-DD, today, yesterday, or '24 Oct' / '24 October'. None if unrecognised."""
    t = (text or "").strip().lower()
    if t == "today":
        return today
    if t == "yesterday":
        return today - timedelta(days=1)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.fullmatch(r"(\d{1,2})\s+([a-z]{3})[a-z]*(?:\s+(\d{4}))?", t)
        if m and m.group(2) in _MONTHS:
            year = int(m.group(3)) if m.group(3) else today.year
            d = date(year, _MONTHS[m.group(2)], int(m.group(1)))
            if not m.group(3) and d > today:
                d = d.replace(year=year - 1)
            return d
    except ValueError:
        return None
    return None
