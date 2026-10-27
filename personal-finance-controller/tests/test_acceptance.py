"""Spec 17 - end-to-end acceptance scenarios A-G, through the full update pipeline.

Claude and Telegram are mocked (FakeInterpreter / FakeSender). SQLite is a real on-disk database and
"restart" closes and reopens it.
"""

import sqlite3
from datetime import date

from krona.briefing import compose, deliver
from krona.claude import InterpreterUnavailable
from krona.finance import compute_allowance, summarise_month
from krona.store import Store
from tests.conftest import USER, item, sgt, tx

TODAY = date(2026, 10, 10)


def test_A_expense_logging(hc):
    hc.interpreter.push(tx("expense", [item("Protein milk", 2, "3.80", None, "bottle", "bottles")],
                           category="Food and groceries"))
    upd = hc.update("Bought 2 bottles of protein milk at $3.80 each", update_id=1001)
    hc.bot.process_update(upd)
    reply = hc.sender.last
    assert reply.splitlines()[0] == "Logged."
    assert reply == "Logged.\nProtein milk, 2 bottles, $7.60"
    row = hc.conn.execute("SELECT * FROM transaction_items").fetchone()
    assert (row["quantity"], row["unit_price_cents"], row["line_total_cents"]) == (2, 380, 760)
    hc.restart()
    hc.bot.process_update(upd)  # reprocessing the same update after restart
    assert hc.tx_count() == 1
    assert "Spent: $7.60" in hc.say("/report")


def test_B_income(hc):
    hc.say("/budget overall 1000")
    hc.say("/budget set Food 300")
    hc.interpreter.push(tx("income", [item("Internship pay", 1, None, "900")], income_kind="salary"))
    assert hc.say("Received $900 internship pay") == "Logged.\nInternship pay, income, $900.00"
    s = summarise_month(hc.store, TODAY)
    assert s.income == 90000 and s.gross_spending == 0 and s.net_cash_flow == 90000
    assert s.overall_limit == 100000 and s.categories[hc.store.resolve_category("Food")["id"]].limit == 30000


def test_C_budget_warning(h):
    h.say("/budget set Food 100")
    h.say("Spent $40 on groceries")
    upd = h.update("Spent $42 on dinner", update_id=3001)
    h.bot.process_update(upd)
    assert h.sender.last == ("Logged.\nDinner, 1 purchase, $42.00\n"
                             "Budget warning: Food and groceries is at 82% of its monthly limit, "
                             "with 32% of the month gone.")
    cu = summarise_month(h.store, TODAY).categories[h.store.resolve_category("Food")["id"]]
    assert (cu.net, cu.remaining, cu.pct) == (8200, 1800, 82)
    sent_before = len(h.sender.sent)
    h.bot.process_update(upd)
    h.bot.process_update(upd)
    assert len(h.sender.sent) == sent_before  # no duplicate spending, no repeated warning
    assert summarise_month(h.store, TODAY).net_spending == 8200


def test_D_purchase_evaluation(hc):
    hc.say("/budget overall 800")
    hc.say("/budget set Shopping 150")
    hc.say("Spent $50 on shoes")
    hc.interpreter.push({"intent": "purchase_question", "transaction": None, "query": None,
                         "clarification_question": None, "reply": None,
                         "purchase": {"item": "Shoes", "price": "120", "recurring": "none", "category": "Shopping",
                                      "need": "unknown", "asks_timing": False}})
    reply = hc.say("Should I buy shoes for $120?")
    assert reply.splitlines()[0] in ("BUY", "WAIT", "LET IT GO")
    assert reply.splitlines()[0] == "WAIT"  # $100 left in Shopping; it fits next month's $150
    assert "Shopping has $100.00 left this month." in reply
    assert "exceeds Shopping's remaining budget by $20.00" in reply
    assert hc.tx_count() == 1


def test_E_daily_briefing(h):
    h.say("/budget overall 660")
    h.say("/budget set Food 200")
    h.set_now(sgt(2026, 10, 9, 19))
    h.say("Spent $22 on dinner")
    h.set_now(sgt(2026, 10, 10, 7, 0))
    assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, 0)) == "sent"
    text = h.sender.last
    assert "Spent $22.00 across 1 purchase." in text
    a = compute_allowance(h.store, TODAY)
    assert a.amount == (66000 - 2200) // 22 and f"${a.amount // 100}.{a.amount % 100:02d}" in text
    assert len(text) < 1200
    h.restart()
    assert deliver(h.store, h.sender, USER, sgt(2026, 10, 10, 7, 1)) == "already-sent"
    assert "Not worked out yet: set a monthly limit first" in compose(Store(h.conn, 5555), TODAY)


def test_F_financial_report(h):
    h.say("/accounts add Main main 2000")
    h.say("/accounts add Savings savings 0")
    h.say("/budget overall 1000")
    h.say("/budget set Shopping 100")
    for m in ("Bought shoes for $120", "Received a $20 refund for shoes", "Spent $15 on lunch",
              "Transferred $200 from Main to Savings", "Received $900 internship pay", "Spent $9 on snacks"):
        h.say(m)
    h.say("/undo 6")
    h.say("yes") if "Reply yes" in h.sender.last else None
    r = h.say("/report")
    assert "Income: $900.00" in r
    assert "Spent: $115.00 (after $20.00 refunds)" in r
    assert "Moved between your accounts: $200.00 (not counted as spending or income)" in r
    assert "Shopping: $100.00 spent, $0.00 left of $100.00 (limit reached)" in r
    assert "Undone entries: 1" in r
    assert "Balances (as you entered them, not live bank data): Main $1,800.00, Savings $200.00" in r
    s = summarise_month(h.store, TODAY)
    assert s.net_cash_flow == 90000 + 2000 - 13500


def test_G_failure_handling(hc, monkeypatch):
    hc.say("Spent $5 on coffee")
    # Database write failure.
    orig = Store.commit_transaction
    monkeypatch.setattr(Store, "commit_transaction",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    hc.interpreter.push(tx("expense", [item("Lunch", 1, None, "12")]))
    assert hc.say("Spent $12 on lunch").startswith("Not saved.")
    monkeypatch.setattr(Store, "commit_transaction", orig)
    # Claude timeout on a message only Claude could understand.
    hc.interpreter.push(InterpreterUnavailable("timeout"))
    assert "nothing was logged" in hc.say("that kopi thing earlier was like four fifty")
    # Telegram send failure: saved once, reply retried later, no duplicate.
    hc.sender.fail_next = [False]
    hc.interpreter.push(tx("expense", [item("Taxi", 1, None, "18")], category="Transport"))
    hc.bot.process_update(hc.update("Taxi $18", update_id=9001))
    hc.bot.retry_unsent()
    assert hc.sender.last == "Logged.\nTaxi, 1 purchase, $18.00"
    # Restart: records intact and a redelivered update is not duplicated.
    hc.restart()
    hc.bot.process_update(hc.update("Taxi $18", update_id=9001))
    descs = [r["description"] for r in hc.conn.execute("SELECT description FROM transactions ORDER BY id")]
    assert descs == ["Coffee", "Taxi"]
    assert all(not m[1].startswith("Logged.\nLunch") for m in hc.sender.sent)
