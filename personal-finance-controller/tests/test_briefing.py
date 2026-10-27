"""Spec 16.6 - daily briefing at 07:00 Asia/Singapore."""

from datetime import date, datetime, timezone


from krona.briefing import RESENT_NOTE, Scheduler, compose, deliver, is_due, seconds_until_next, tick
from tests.conftest import USER, sgt


def briefings(h):
    return [m[1] for m in h.sender.sent if "DAILY BRIEFING" in m[1]]


def test_timezone_is_singapore():
    # 23:00 UTC on 9 Oct is 07:00 on 10 Oct in Singapore.
    now = datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc)
    assert is_due(now, 7, 12)
    assert not is_due(datetime(2026, 10, 9, 22, 59, tzinfo=timezone.utc), 7, 12)
    assert seconds_until_next(datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc), 7) == 3600
    assert not is_due(sgt(2026, 10, 10, 13, 0), 7, 12)  # past the catch-up window


def test_previous_calendar_day_and_midnight_boundary(h):
    h.set_now(sgt(2026, 10, 9, 23, 59))
    h.say("Spent $10 on supper")           # counts for 9 Oct
    h.set_now(sgt(2026, 10, 10, 0, 1))
    h.say("Spent $7 on breakfast")         # counts for 10 Oct, not "yesterday" on 10 Oct
    text = compose(h.store, date(2026, 10, 10))
    assert "Spent $10.00 across 1 purchase." in text and "Supper" in text and "Breakfast" not in text


def test_month_boundary(h):
    h.set_now(sgt(2026, 10, 31, 20))
    h.say("Spent $25 on dinner")
    h.say("/budget overall 300")
    text = compose(h.store, date(2026, 11, 1))
    assert "Spent $25.00 across 1 purchase." in text
    # New month: the full limit is available again over 30 days.
    assert "$10.00 today" in text and "$300.00 left this month spread over 30 days" in text


def test_backdated_transaction_counted_on_its_date(h):
    h.say("I spent $18.50 on dinner yesterday")
    assert "Spent $18.50" in compose(h.store, date(2026, 10, 10))


def test_no_transactions_recorded(h):
    text = compose(h.store, date(2026, 10, 10))
    assert "- No transactions were recorded." in text
    assert "no money" not in text.lower()


def test_incomplete_budgets_disclosed(h):
    text = compose(h.store, date(2026, 10, 10))
    assert "Not worked out yet: set a monthly limit first" in text
    h.say("/budget set Food 300")
    text = compose(h.store, date(2026, 10, 10))
    assert "No overall monthly limit yet, so only per-category figures" in text and "a day" in text


def test_category_approaching_and_over_limit(h):
    h.say("/budget overall 1000")
    h.say("/budget set Food 100")
    h.set_now(sgt(2026, 10, 9))
    h.say("Spent $80 on groceries")
    text = compose(h.store, date(2026, 10, 10))
    assert "Food and groceries: 80% used with 22 days left. $20.00 remains." in text
    assert "Cap Food and groceries at $0.90 today." in text
    h.say("Spent $30 on dinner")
    text = compose(h.store, date(2026, 10, 10))
    assert "Limit used up" in text and "Spend nothing more on Food and groceries" in text


def test_no_category_concerning(h):
    h.say("/budget overall 1000")
    h.say("/budget set Food 500")
    h.set_now(sgt(2026, 10, 9))
    h.say("Spent $10 on lunch")
    assert "No category needs attention." in compose(h.store, date(2026, 10, 10))


def test_no_discretionary_allowance_remaining(h):
    h.say("/budget overall 100")
    h.set_now(sgt(2026, 10, 9))
    h.say("Spent $100 on lunch")
    text = compose(h.store, date(2026, 10, 10))
    assert "$0.00. Nothing is left in this month's limit." in text and "Spend nothing discretionary today." in text


def test_upcoming_commitments_reduce_allowance(h):
    h.say("/budget overall 1100")
    h.say("/budget commit add Rent 440 day 28")
    text = compose(h.store, date(2026, 10, 10))
    assert "$30.00 today" in text and "after setting aside $440.00 for bills" in text


def test_refunds_and_reversals_in_yesterday(h):
    h.set_now(sgt(2026, 10, 9))
    h.say("Bought shoes for $120")
    h.say("Received a $20 refund for shoes")
    h.say("Spent $9 on lunch")
    h.say("/undo 3")
    text = compose(h.store, date(2026, 10, 10))
    assert "Spent $120.00 across 1 purchase; $20.00 refunded, net $100.00." in text
    assert "Lunch" not in text


def test_deterministic_allowance(h):
    h.say("/budget overall 660")
    a = compose(h.store, date(2026, 10, 10))
    b = compose(h.store, date(2026, 10, 10))
    assert a == b and "$30.00 today" in a  # 660 / 22


def test_no_duplicate_daily_delivery(h):
    assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, 0)) == "sent"
    for minute in (0, 1, 30):
        assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, minute)) == "already-sent"
    assert len(briefings(h)) == 1
    row = h.store.briefing(date(2026, 10, 10))
    assert row["status"] == "sent" and row["telegram_message_id"] is not None


def test_scheduler_restart_does_not_resend(h):
    h.set_now(sgt(2026, 10, 10, 6, 59))
    assert tick(h.store, h.sender, USER, h.clock, 7, 12) == "not-due"
    h.set_now(sgt(2026, 10, 10, 7, 0, ))
    assert tick(h.store, h.sender, USER, h.clock, 7, 12) == "sent"
    h.restart()
    assert tick(h.store, h.sender, USER, h.clock, 7, 12) == "already-sent"
    assert len(briefings(h)) == 1


def test_catch_up_after_late_start(h):
    h.set_now(sgt(2026, 10, 10, 9, 15))  # host was off at 07:00
    assert tick(h.store, h.sender, USER, h.clock, 7, 12) == "sent"


def test_telegram_send_failure_and_safe_retry(h):
    h.sender.fail_next = [False]  # definite failure (Telegram rejected / unreachable)
    assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, 0)) == "failed"
    assert h.store.briefing(date(2026, 10, 10))["status"] == "failed"
    assert briefings(h) == []
    assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, 0, )) == "backoff"
    assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, 2)) == "sent"
    sent = briefings(h)
    assert len(sent) == 1 and RESENT_NOTE not in sent[0]  # first copy that actually arrived


def test_uncertain_delivery_retry_is_marked_not_silent(h):
    h.sender.fail_next = [True]  # timed out after sending: Telegram may have it
    assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, 0)) == "uncertain"
    assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, 5)) == "backoff"
    assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, 11)) == "sent"
    assert RESENT_NOTE in briefings(h)[-1]


def test_crash_mid_send_is_reconciled_on_restart(h):
    h.store.claim_briefing(date(2026, 10, 10))  # 'sending' row left behind by a crash
    h.restart()
    assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, 1)) == "sent"
    assert RESENT_NOTE in briefings(h)[-1]


def test_briefing_format_and_compactness(h):
    h.say("/budget overall 900")
    text = compose(h.store, date(2026, 10, 27))
    lines = text.splitlines()
    assert lines[0] == "KRONA | DAILY BRIEFING" and lines[1] == "Tuesday, 27 October 2026"
    for heading in ("YESTERDAY", "TIGHTEN UP TODAY", "FREE TO SPEND TODAY", "VERDICT"):
        assert heading in lines
    assert len(text) < 1200



def test_scheduler_thread_delivers_once(tmp_path):
    import threading
    from krona.db import open_db
    from krona.store import Store
    from krona.timeutil import fixed_clock
    from tests.conftest import FakeSender

    db = tmp_path / "k.db"
    clock = fixed_clock(sgt(2026, 10, 10, 7, 0, ))
    sender = FakeSender()
    stop = threading.Event()
    sched = Scheduler(lambda: Store(open_db(db), USER), sender, USER, clock, 7, 12, stop)
    sched.start()
    for _ in range(100):
        if sender.sent:
            break
        stop.wait(0.05)
    stop.set()
    sched.join(timeout=5)
    assert len(sender.sent) == 1 and "DAILY BRIEFING" in sender.sent[0][1]
    assert sched.clock.now().tzinfo is not None and sched.clock.now().utcoffset().total_seconds() == 8 * 3600
