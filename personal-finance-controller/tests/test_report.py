"""Spec 16.5 - /report."""

from datetime import date

from krona.finance import summarise_month
from krona.report import build_report
from tests.conftest import sgt

TODAY = date(2026, 10, 10)


def test_empty_database(h):
    r = h.say("/report")
    assert "Income received: $0.00" in r and "No transactions recorded for this period." in r
    assert "Overall limit: not set" in r and "Balances: no accounts recorded" in r


def test_income_without_spending(h):
    h.say("Received $900 internship pay")
    r = h.say("/report")
    assert "Income received: $900.00" in r and "Consumption spending: $0.00 gross" in r
    assert "Net cash flow: $900.00" in r


def test_spending_without_income(h):
    h.say("Spent $50 on lunch")
    r = h.say("/report")
    assert "Income received: $0.00" in r and "Net cash flow: -$50.00" in r


def test_refunds_and_reversals(h):
    h.say("Bought shoes for $120")
    h.say("Received a $20 refund for shoes")
    h.say("Spent $30 on lunch")
    h.say("/undo 3")
    r = h.say("/report")
    assert "Consumption spending: $120.00 gross, $100.00 net of $20.00 refunds" in r
    assert "Reversed entries: 1" in r


def test_over_budget_category(h):
    h.say("/budget set Food 50")
    h.say("Spent $60 on dinner")
    r = h.say("/report")
    assert "Food and groceries: $60.00 of $50.00 (120%), OVER" in r
    assert "Stop discretionary spending in Food and groceries; it is $10.00 over." in r


def test_missing_account_balances_disclosed(h):
    assert "no accounts recorded" in h.say("/report")
    h.say("/accounts add Main main 1000")
    assert "Balances (user-maintained, not live bank data):" in h.say("/report")


def test_current_and_previous_months(h):
    h.set_now(sgt(2026, 9, 15))
    h.say("Spent $70 on lunch")
    h.set_now(sgt(2026, 10, 10))
    h.say("Spent $20 on lunch")
    assert "Consumption spending: $20.00 gross" in h.say("/report")
    last = h.say("/report last")
    assert "September 2026" in last and "Consumption spending: $70.00 gross" in last
    assert "September 2026" in h.say("/report 2026-09")


def test_report_reconciles_with_underlying_transactions(h):
    h.say("/accounts add Main main 1000")
    h.say("/accounts add Savings savings 0")
    for m in ("Spent $12 on lunch", "Paid $25 for transport", "Received $900 internship pay",
              "Transferred $100 from Main to Savings", "Bought shoes for $50", "Received a $10 refund for shoes"):
        h.say(m)
    s = summarise_month(h.store, TODAY)
    rows = h.conn.execute("SELECT type, total_cents FROM transactions WHERE status='active'").fetchall()
    exp = sum(r["total_cents"] for r in rows if r["type"] == "expense")
    inc = sum(r["total_cents"] for r in rows if r["type"] == "income")
    ref = sum(r["total_cents"] for r in rows if r["type"] == "refund")
    assert (s.gross_spending, s.income, s.refunds) == (exp, inc, ref)
    assert s.net_cash_flow == inc + ref - exp
    r = build_report(h.store, TODAY)
    assert "Transfers between own accounts: $100.00 (excluded from income and spending" in r


def test_followups_use_same_engine(h):
    h.say("/budget set Food 100")
    h.say("Spent $40 on lunch")
    assert "Food and groceries, October to date: $40.00 across 1 purchases" in h.say("Show food spending")
    assert "FREE TO SPEND TODAY" in h.say("How much can I safely spend today?")
    assert "No transactions recorded for September" in h.say("Compare this month to last month")


def test_why_overspending_guarded_explanation(hc):
    hc.say("/budget set Food 100")
    hc.say("Spent $80 on lunch")
    from krona.claude import InterpreterUnavailable
    from krona.parser import parse_query
    hc.interpreter.push(parse_query("why am I overspending?"))
    hc.interpreter.explain_reply = "Food and groceries is at 80% of $100.00 by day 10; lunch drove it."
    assert hc.say("why am I overspending?") == hc.interpreter.explain_reply
    # An explanation that invents a figure is discarded for the deterministic facts.
    hc.interpreter.push(parse_query("why am I overspending?"))
    hc.interpreter.explain_reply = "You spent $999 on lunch."
    reply = hc.say("why am I overspending?")
    assert "$999" not in reply and "ahead of pace" in reply
    hc.interpreter.push(parse_query("why am I overspending?"))
    hc.interpreter.explain_reply = InterpreterUnavailable("down")
    assert "ahead of pace" in hc.say("why am I overspending?")


def test_long_report_is_split(h):
    from krona.fmt import split_message
    text = "\n\n".join(["SECTION\n" + "x" * 1500] * 5)
    parts = split_message(text)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)
