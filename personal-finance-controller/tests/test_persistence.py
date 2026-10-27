"""Spec 16.3 - persistence, idempotency, failure handling."""

import sqlite3
from datetime import date

from krona.finance import summarise_month
from krona.report import build_report
from krona.store import Store


def test_transaction_survives_restart(h):
    h.say("Spent $12 on lunch")
    h.restart()
    assert h.tx_count() == 1
    assert "Lunch" in h.say("/history")


def test_budget_survives_restart(h):
    h.say("/budget set Food 100")
    h.say("/budget overall 900")
    h.restart()
    assert h.store.active_limits() == {h.store.resolve_category("Food")["id"]: 10000, None: 90000}


def test_migrations_never_wipe_data(h):
    from krona.db import migrate
    h.say("Spent $12 on lunch")
    migrate(h.conn)
    migrate(h.conn)
    assert h.tx_count() == 1


def test_same_update_processed_twice(h):
    upd = h.update("Spent $12 on lunch", update_id=500)
    h.bot.process_update(upd)
    h.bot.process_update(upd)
    h.restart()
    h.bot.process_update(upd)
    assert h.tx_count() == 1
    assert len([m for m in h.sender.sent if m[1].startswith("Logged.")]) == 1


def test_two_legitimate_identical_purchases(h):
    h.say("Spent $3 on coffee")
    reply = h.say("Spent $3 on coffee")
    assert "Log it again as a second purchase?" in reply
    assert h.tx_count() == 1
    assert h.say("yes") == "Logged.\nCoffee, 1 purchase, $3.00"
    assert h.tx_count() == 2


def test_accidental_duplicate_declined(h):
    h.say("Spent $3 on coffee")
    h.say("Spent $3 on coffee")
    assert h.say("no") == "OK, not logged again."
    assert h.tx_count() == 1


def test_database_write_failure_reports_not_saved(h, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(Store, "commit_transaction", boom)
    reply = h.say("Spent $12 on lunch")
    assert reply.startswith("Not saved.") and "Logged." not in reply
    assert h.tx_count() == 0


def test_database_failure_mid_write_rolls_back(h, monkeypatch):
    orig = Store._insert_processed

    def fail_after_tx_insert(self, *a, **k):
        raise sqlite3.OperationalError("simulated failure after transaction row")
    monkeypatch.setattr(Store, "_insert_processed", fail_after_tx_insert)
    reply = h.say("Spent $12 on lunch")
    monkeypatch.setattr(Store, "_insert_processed", orig)
    assert reply.startswith("Not saved.")
    # Atomic: the transaction row and its lines were rolled back with the idempotency record.
    assert h.tx_count() == 0
    assert h.conn.execute("SELECT COUNT(*) n FROM transaction_items").fetchone()["n"] == 0


def test_failure_after_commit_is_reported_as_logged(h, monkeypatch):
    """If an error happens after the commit, KRONA checks the database rather than claiming failure."""
    import krona.bot as botmod

    def boom(*a, **k):
        raise sqlite3.OperationalError("alert table locked")
    monkeypatch.setattr(botmod, "check_after_commit", boom)
    h.say("/budget set Food 100")
    reply = h.say("Spent $90 on lunch")
    assert reply.startswith("Logged.") and h.tx_count() == 1


def test_edit_and_reversal_are_audited(h):
    h.say("Spent $12 on lunch")
    h.say("/edit 1 amount 15")
    h.say("/undo 2")
    events = [(r["transaction_id"], r["event"]) for r in h.conn.execute("SELECT * FROM transaction_events ORDER BY id")]
    assert events == [(1, "created"), (1, "corrected"), (2, "created"), (2, "reversed")]
    assert h.conn.execute("SELECT status FROM transactions WHERE id = 1").fetchone()["status"] == "corrected"
    assert h.tx_count() == 2  # nothing deleted


def test_report_correct_after_editing(h):
    h.say("/budget set Food 100")
    h.say("Spent $12 on lunch")
    h.say("/edit 1 amount 30")
    s = summarise_month(h.store, date(2026, 10, 10))
    assert s.net_spending == 3000
    assert "Food and groceries: $30.00 spent, $70.00 left of $100.00 (on track)" in build_report(h.store, date(2026, 10, 10))


def test_edit_fields(h):
    h.say("Bought 2 bottles of protein milk at $3.80 each")
    assert "3 bottles, $11.40" in h.say("/edit 1 quantity 3")
    assert "3 bottles, $12.00" in h.say("/edit 2 price 4")
    assert "Whey milk, 3 bottles" in h.say("/edit 3 description Whey milk")
    h.say("/edit 4 category Shopping")
    h.say("/edit 5 date 2026-10-08")
    t = h.store.recent(1)[0]
    assert t["effective_date"] == "2026-10-08" and h.store.category(t["category_id"])["name"] == "Shopping"
    assert "future" in h.say("/edit 6 date 2026-12-01")


def test_daily_briefing_record_survives_restart(h):
    from krona.briefing import deliver
    from tests.conftest import USER, sgt
    assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, 0)) == "sent"
    h.restart()
    assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, 5)) == "already-sent"
    assert len([m for m in h.sender.sent if "DAILY BRIEFING" in m[1]]) == 1


def test_undo_multiple_candidates_asks_to_choose(h):
    h.say("Spent $12 on lunch")
    h.say("Spent $14 on lunch with team")
    reply = h.say("/undo lunch")
    assert reply.startswith("Which one should I undo?")
    assert h.say("2").startswith("Undone: Lunch $12.00")
    assert h.tx_count("reversed") == 1
