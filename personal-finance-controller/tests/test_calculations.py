"""Spec 16.2 - deterministic calculations."""

from datetime import date

from krona.finance import compute_allowance, summarise_month
from tests.conftest import sgt


def food(h):
    return h.store.resolve_category("Food")["id"]


def test_category_totals_remaining_and_percentages(h):
    h.say("/budget set Food 100")
    h.say("Spent $40 on lunch")
    h.say("Spent $42 on dinner")
    s = summarise_month(h.store, date(2026, 10, 10))
    cu = s.categories[food(h)]
    assert (cu.gross, cu.net, cu.limit, cu.remaining, cu.pct) == (8200, 8200, 10000, 1800, 82)


def test_overall_limit_and_percentage(h):
    h.say("/budget overall 500")
    h.say("Spent $100 on lunch")
    h.say("Paid $25 for transport")
    s = summarise_month(h.store, date(2026, 10, 10))
    assert (s.net_spending, s.overall_remaining, s.overall_pct) == (12500, 37500, 25)


def test_refund_reduces_original_category(h):
    h.say("/budget set Shopping 200")
    h.say("Bought shoes for $120")
    h.say("Received a $20 refund for shoes")
    s = summarise_month(h.store, date(2026, 10, 10))
    cu = s.categories[h.store.resolve_category("Shopping")["id"]]
    assert (cu.gross, cu.refunds, cu.net, cu.remaining) == (12000, 2000, 10000, 10000)
    assert (s.gross_spending, s.refunds, s.net_spending) == (12000, 2000, 10000)


def test_transfers_excluded_from_income_and_spending(h):
    h.say("/accounts add Main main 1000")
    h.say("/accounts add Savings savings 0")
    h.say("Transferred $300 from Main to Savings")
    s = summarise_month(h.store, date(2026, 10, 10))
    assert (s.income, s.gross_spending, s.net_cash_flow) == (0, 0, 0)
    assert s.transfers_total == 30000 and s.transfers_to_savings == 30000


def test_savings_category_is_allocation_not_consumption(hc):
    from tests.conftest import item, tx
    hc.interpreter.push(tx("expense", [item("ETF purchase", 1, None, "500")], category="Savings and investments"))
    hc.say("Bought $500 of ETF")
    s = summarise_month(hc.store, date(2026, 10, 10))
    assert s.gross_spending == 0 and s.savings_outflows == 50000 and s.net_cash_flow == -50000


def test_calendar_month_boundaries_singapore(h):
    # 31 Oct 23:30 SGT is still October in Singapore (15:30 UTC).
    h.set_now(sgt(2026, 10, 31, 23, 30))
    h.say("Spent $10 on supper")
    h.set_now(sgt(2026, 11, 1, 0, 15))  # 31 Oct 16:15 UTC, but 1 Nov in Singapore
    h.say("Spent $20 on breakfast")
    oct_ = summarise_month(h.store, date(2026, 10, 1))
    nov = summarise_month(h.store, date(2026, 11, 1))
    assert oct_.net_spending == 1000 and nov.net_spending == 2000


def test_income_does_not_expand_budget(h):
    h.say("/budget overall 500")
    h.say("/budget set Food 100")
    h.say("Received $900 internship pay")
    s = summarise_month(h.store, date(2026, 10, 10))
    assert s.overall_limit == 50000 and s.categories[food(h)].limit == 10000
    assert s.income == 90000 and s.net_cash_flow == 90000


def test_missing_budget_configuration(h):
    h.say("Spent $12 on lunch")
    s = summarise_month(h.store, date(2026, 10, 10))
    assert s.overall_limit is None and s.overall_remaining is None
    a = compute_allowance(h.store, date(2026, 10, 10))
    assert a.status == "unavailable" and a.amount is None


def test_safe_to_spend_no_double_counting(h):
    # 10 Oct: 22 days left including today. Overall 660, category limits sum to more than overall.
    h.say("/budget overall 660")
    h.say("/budget set Food 500")
    h.say("/budget set Shopping 500")
    h.set_now(sgt(2026, 10, 9))
    h.say("Spent $110 on lunch")
    h.set_now(sgt(2026, 10, 10))
    a = compute_allowance(h.store, date(2026, 10, 10))
    # (660 - 110) / 22 = 25.00 - category limits never add money on top of the overall limit.
    assert a.amount == 2500 and a.overall_left == 55000 and a.status == "available"


def test_category_budget_not_permission_to_exceed_overall(h):
    h.say("/budget overall 100")
    h.say("/budget set Shopping 1000")
    h.set_now(sgt(2026, 10, 9))
    h.say("Spent $100 on lunch")
    h.set_now(sgt(2026, 10, 10))
    a = compute_allowance(h.store, date(2026, 10, 10))
    assert a.amount == 0 and a.status == "exhausted"


def test_overall_budget_not_permission_to_breach_category(h):
    h.say("/budget overall 2000")
    h.say("/budget set Food 100")
    h.set_now(sgt(2026, 10, 9))
    h.say("Spent $100 on lunch")
    h.set_now(sgt(2026, 10, 10))
    a = compute_allowance(h.store, date(2026, 10, 10))
    assert a.amount > 0 and "Food and groceries" in a.exhausted_categories


def test_committed_money_excluded_from_free_to_spend(h):
    h.say("/budget overall 1100")
    h.say("/budget commit add Rent 440 day 25")
    a = compute_allowance(h.store, date(2026, 10, 10))
    assert a.committed == 44000 and a.overall_left == 66000 and a.amount == 66000 // 22
    # Paying it (logged with a matching description) releases the reservation and counts as spending.
    reply = h.say("Paid $440 for rent")
    assert "Counted as this month's Rent payment." in reply
    h.set_now(sgt(2026, 10, 11))
    a2 = compute_allowance(h.store, date(2026, 10, 11))
    assert a2.committed == 0 and a2.overall_left == 66000


def test_protected_funds_and_balances_cap_allowance(h):
    h.say("/budget overall 3000")
    h.say("/accounts add Main main 1000")
    h.say("/goals protect 780")
    a = compute_allowance(h.store, date(2026, 10, 10))
    # Cash: 1000 - 780 protected = 220 over 22 days = $10/day, lower than the budget figure.
    assert a.binding == "cash" and a.amount == 1000


def test_allowance_recalculated_not_accumulated(h):
    h.say("/budget overall 310")
    h.set_now(sgt(2026, 10, 1))
    a1 = compute_allowance(h.store, date(2026, 10, 1))
    assert a1.amount == 1000  # 310 / 31
    # Nothing spent for days: tomorrow's figure is recalculated from what is actually left.
    a5 = compute_allowance(h.store, date(2026, 10, 5))
    assert a5.amount == 31000 // 27


def test_quantity_unit_price_calculation_exact(hc):
    from tests.conftest import item, tx
    hc.interpreter.push(tx("expense", [item("Can of drink", 3, "1.10")]))
    hc.say("3 cans at 1.10 each")
    assert hc.conn.execute("SELECT total_cents FROM transactions").fetchone()["total_cents"] == 330
