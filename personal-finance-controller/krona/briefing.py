"""KRONA daily briefing (07:00 Asia/Singapore) and its delivery scheduler.

Delivery protocol, per (user, date, 'daily'):
  1. If a row is 'sent', do nothing.
  2. If a row is 'sending' with no outcome (crash mid-send) or 'uncertain' (Telegram may have received it),
     the outcome cannot be queried from the Bot API, so a retry is sent with an explicit
     "Resent" marker. A duplicate is therefore never silent.
  3. Mark 'sending' in the database before calling Telegram; mark 'sent' only after Telegram returns a
     message id. Definite failures become 'failed' and are retried with backoff until the catch-up hour.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone

from .finance import compute_allowance, elapsed_pct, is_consumption, projected, summarise_month
from .money import fmt
from .report import allowance_lines
from .store import Store
from .telegram import Sender, TelegramSendError
from .timeutil import SGT, Clock, at_sgt, days_in_month, long_date, month_bounds

log = logging.getLogger("krona.briefing")

RETRY_MINUTES = (1, 2, 5, 10, 15, 30)
RESENT_NOTE = "(Resent: delivery of the earlier copy could not be confirmed.)"


def _yesterday_section(store: Store, today: date) -> list[str]:
    y = today - timedelta(days=1)
    start_y = month_bounds(y)[0]
    upto = summarise_month(store, y, through=y)
    before = summarise_month(store, y, through=y - timedelta(days=1)) if y > start_y else None
    rows = store.active_between(y, y)
    if not rows:
        return ["No transactions were recorded."]
    net = upto.net_spending - (before.net_spending if before else 0)
    refunds = upto.refunds - (before.refunds if before else 0)
    expenses = [r for r in rows if r["type"] == "expense" and is_consumption(r["category_kind"])]
    gross = net + refunds
    lines = [f"Spent {fmt(gross)} across {len(expenses)} purchase{'s' if len(expenses) != 1 else ''}"
             + (f"; {fmt(refunds)} refunded, net {fmt(net)}." if refunds else ".")]
    by_cat: dict[str, int] = {}
    for r in expenses:
        by_cat[r["category_name"] or "Uncategorised"] = by_cat.get(r["category_name"] or "Uncategorised", 0) + r["total_cents"]
    if expenses:
        top_cat = max(by_cat.items(), key=lambda kv: kv[1])
        biggest = max(expenses, key=lambda r: r["total_cents"])
        if len(by_cat) > 1:
            lines.append(f"Largest category: {top_cat[0]}, {fmt(top_cat[1])}.")
        lines.append(f"Largest expense: {biggest['description']}, {fmt(biggest['total_cents'])}.")
    income = sum(r["total_cents"] for r in rows if r["type"] == "income")
    if income:
        lines.append(f"Income recorded: {fmt(income)}.")
    elif before is not None and y.day >= 4 and before.net_spending:
        avg = before.net_spending // (y.day - 1)
        if avg and net > avg * 3 // 2:
            lines.append(f"Above this month's average day of {fmt(avg)}.")
        elif avg and net * 2 < avg:
            lines.append(f"Below this month's average day of {fmt(avg)}.")
    return lines


def _tighten_section(store: Store, today: date) -> tuple[list[str], str | None]:
    s = summarise_month(store, today, through=today - timedelta(days=1))
    days_left = days_in_month(today) - today.day + 1
    limited = [c for c in s.categories.values() if c.limit and is_consumption(c.kind)]
    if not limited and not s.overall_limit:
        return ["No limits set, so nothing can be measured. Set one with /budget set Food 400."], None
    worst = None
    worst_score = -1
    for c in limited:
        p = c.pct or 0
        proj = projected(c.net, today - timedelta(days=1)) if today.day > 1 else c.net
        score = p if p >= 100 else (p - elapsed_pct(today) if proj > c.limit else -1)
        if score > worst_score and (p >= 75 or proj > c.limit):
            worst, worst_score = c, score
    if worst is None:
        if s.overall_limit and (s.overall_pct or 0) > elapsed_pct(today):
            left = max(0, s.overall_remaining or 0)
            return [f"Overall spending is at {s.overall_pct}% with {elapsed_pct(today)}% of the month gone.",
                    f"Keep total spending under {fmt(left // days_left)} a day."], "overall"
        return ["No category needs attention."], None
    left = worst.limit - worst.net
    if left <= 0:
        return [f"{worst.name}: {fmt(worst.net)} of {fmt(worst.limit)} ({worst.pct}%). Limit used up.",
                f"Spend nothing more on {worst.name} this month."], worst.name
    return [f"{worst.name}: {worst.pct}% used with {days_left} days left. {fmt(left)} remains.",
            f"Cap {worst.name} at {fmt(left // days_left)} today."], worst.name


def compose(store: Store, today: date) -> str:
    y_lines = _yesterday_section(store, today)
    t_lines, focus = _tighten_section(store, today)
    a = compute_allowance(store, today)
    f_lines = allowance_lines(a)
    if a.status == "exhausted":
        verdict = "Spend nothing discretionary today."
    elif a.status in ("unavailable",):
        verdict = "Log every purchase today, and set an overall limit so KRONA can give you a daily number."
    elif a.status == "provisional":
        verdict = "Stay inside each category's daily figure; set an overall limit for a firm number."
    elif focus and focus != "overall":
        verdict = f"Stay within {fmt(a.amount)} today and keep {focus} to the minimum."
    else:
        verdict = f"On track. Stay within {fmt(a.amount)} today."
    out = ["KRONA | DAILY BRIEFING", long_date(today), "",
           "YESTERDAY", *[f"- {x}" for x in y_lines], "",
           "TIGHTEN UP TODAY", *[f"- {x}" for x in t_lines], "",
           "FREE TO SPEND TODAY", *[f"- {x}" for x in f_lines], "",
           "VERDICT", f"- {verdict}"]
    return "\n".join(out)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def deliver(store: Store, sender: Sender, chat_id: int, now: datetime) -> str:
    """Attempt today's briefing if due. Returns the resulting state (or a skip reason)."""
    today = now.astimezone(SGT).date()
    row = store.briefing(today)
    if row is not None:
        if row["status"] == "sent":
            return "already-sent"
        if row["status"] in ("failed", "uncertain") and row["next_attempt_at"] and row["next_attempt_at"] > _iso(now):
            return "backoff"
    previously_unclear = row is not None and row["status"] in ("sending", "uncertain")
    text = compose(store, today)
    if previously_unclear:
        text = text + "\n\n" + RESENT_NOTE
    claimed = store.claim_briefing(today)
    if claimed["status"] == "sent":
        return "already-sent"
    try:
        message_id = sender.send(chat_id, text)
    except TelegramSendError as exc:
        if exc.uncertain:
            store.finish_briefing(today, "uncertain", error=str(exc),
                                  next_attempt_at=_iso(now + timedelta(minutes=10)))
            log.warning("daily briefing delivery uncertain: %s", exc)
            return "uncertain"
        n = min(claimed["attempts"], len(RETRY_MINUTES)) - 1
        nxt = _iso(now + timedelta(minutes=RETRY_MINUTES[max(0, n)]))
        store.finish_briefing(today, "failed", error=str(exc), next_attempt_at=nxt)
        log.warning("daily briefing delivery failed: %s", exc)
        return "failed"
    except Exception as exc:  # unexpected - record without claiming success
        store.finish_briefing(today, "failed", error=type(exc).__name__,
                              next_attempt_at=_iso(now + timedelta(minutes=5)))
        log.exception("daily briefing delivery crashed")
        return "failed"
    store.finish_briefing(today, "sent", message_id=message_id)
    log.info("daily briefing sent for %s", today.isoformat())
    return "sent"


def is_due(now: datetime, hour: int, catchup_until_hour: int) -> bool:
    local = now.astimezone(SGT)
    start = at_sgt(local.date(), hour)
    end = at_sgt(local.date(), catchup_until_hour, 59)
    return start <= local <= end


def seconds_until_next(now: datetime, hour: int) -> float:
    local = now.astimezone(SGT)
    target = at_sgt(local.date(), hour)
    if local >= target:
        target = at_sgt(local.date() + timedelta(days=1), hour)
    return (target - local).total_seconds()


def tick(store: Store, sender: Sender, chat_id: int, clock: Clock, hour: int, catchup: int) -> str:
    now = clock.now()
    if not is_due(now, hour, catchup):
        return "not-due"
    return deliver(store, sender, chat_id, now)


class Scheduler(threading.Thread):
    """Single in-process scheduler. Exactly one runs per KRONA process (enforced by the process lock)."""

    def __init__(self, store_factory, sender: Sender, chat_id: int, clock: Clock, hour: int, catchup: int,
                 stop: threading.Event):
        super().__init__(name="krona-scheduler", daemon=True)
        self.store_factory = store_factory
        self.sender = sender
        self.chat_id = chat_id
        self.clock = clock
        self.hour = hour
        self.catchup = catchup
        self.stop_event = stop

    def run(self) -> None:
        store = self.store_factory()  # this thread's own SQLite connection
        log.info("scheduler started: daily briefing at %02d:00 Asia/Singapore", self.hour)
        while not self.stop_event.is_set():
            try:
                tick(store, self.sender, self.chat_id, self.clock, self.hour, self.catchup)
            except Exception:
                log.exception("scheduler tick failed")
            wait = min(30.0, max(1.0, seconds_until_next(self.clock.now(), self.hour)))
            self.stop_event.wait(wait)
