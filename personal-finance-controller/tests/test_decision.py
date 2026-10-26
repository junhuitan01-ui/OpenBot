"""Spec 16.4 - should i buy."""

from krona.claude import InterpreterUnavailable


def purchase(item, price, recurring="none", category=None, need="unknown", timing=False):
    return {"intent": "purchase_question", "transaction": None, "query": None, "clarification_question": None,
            "reply": None, "purchase": {"item": item, "price": price, "recurring": recurring, "category": category,
                                        "need": need, "asks_timing": timing}}


def test_affordable_purchase_buy(h):
    h.say("/budget overall 1000")
    h.say("/budget set Shopping 300")
    reply = h.say("should i buy shoes for $50?")
    assert reply.splitlines()[0] == "BUY"
    assert "Shopping has $300.00 left" in reply and h.tx_count() == 0


def test_purchase_exceeding_category_limit(h):
    h.say("/budget overall 1000")
    h.say("/budget set Shopping 100")
    h.say("Spent $40 on shoes")
    reply = h.say("should i buy shoes for $120?")
    assert reply.splitlines()[0] == "LET IT GO"  # more than a whole month's Shopping limit
    assert "exceeds Shopping's remaining budget by $60.00" in reply
    assert h.tx_count() == 1


def test_purchase_exceeding_category_now_but_fits_next_month(h):
    h.say("/budget set Shopping 150")
    h.say("Spent $100 on shoes")
    reply = h.say("should i buy a bag for $80?")
    assert reply.splitlines()[0] == "WAIT" and "next month" in reply


def test_purchase_exceeding_overall_limit(h):
    h.say("/budget overall 200")
    h.say("Spent $150 on lunch")
    reply = h.say("should i buy shoes for $120?")
    assert reply.splitlines()[0] == "WAIT"
    assert "exceeds the overall remaining budget by $70.00" in reply


def test_recurring_subscription(hc):
    hc.say("/budget overall 1000")
    hc.say("/budget set Bills 100")
    hc.interpreter.push(purchase("Streaming service", "20", "monthly", "Bills and subscriptions"))
    reply = hc.say("Should I subscribe to this service for $20 a month?")
    assert reply.splitlines()[0] == "BUY" and "$20.00/month, $240.00/year" in reply
    hc.interpreter.push(purchase("Premium gym", "40", "monthly", "Bills and subscriptions"))
    reply = hc.say("Should I subscribe to a gym for $40 a month?")
    assert reply.splitlines()[0] == "WAIT" and "40% of the monthly limit" in reply


def test_missing_financial_data_conditional(h):
    reply = h.say("should i buy a $45 game?")
    assert reply.splitlines()[0] == "WAIT"
    assert "No budget or balances are set" in reply and "Conditional" in reply


def test_no_invented_balances(h):
    h.say("/budget overall 1000")
    reply = h.say("should i buy a $45 game?")
    assert "balance" not in reply.lower()


def test_cash_shortfall_let_it_go_or_wait_for_payday(h):
    h.say("/budget overall 2000")
    h.say("/accounts add Main main 100")
    assert h.say("should i buy shoes for $120?").splitlines()[0] == "LET IT GO"
    h.say("/budget payday 2026-10-25")
    reply = h.say("should i buy shoes for $120?")
    assert reply.splitlines()[0] == "WAIT" and "Next income expected 25 Oct" in reply


def test_large_share_of_remaining_waits(h):
    h.say("/budget overall 200")
    assert h.say("should i buy a jacket for $150?").splitlines()[0] == "WAIT"


def test_cooling_off_rule(h):
    h.say("/budget overall 2000")
    h.say("/budget cooloff 100 48")
    reply = h.say("should i buy headphones for $120?")
    assert reply.splitlines()[0] == "WAIT" and "48 hours" in reply


def test_purchase_question_never_creates_transaction(hc):
    hc.interpreter.push(purchase("Shoes", "120"))
    hc.say("Should I buy shoes for $120?")
    hc.interpreter.push(InterpreterUnavailable("down"))
    hc.say("should i buy shoes for $120?")
    assert hc.tx_count() == 0


def test_purchase_missing_price_asks(hc):
    hc.interpreter.push(purchase("These shoes", None))
    assert hc.say("should I buy these shoes?") == "What is the price?"


def test_claude_api_failure_still_answers_simple_question(hc):
    hc.say("/budget overall 1000")
    hc.interpreter.push(InterpreterUnavailable("timeout"))
    reply = hc.say("should i buy shoes for $120?")
    assert reply.splitlines()[0] in ("BUY", "WAIT", "LET IT GO") and hc.tx_count() == 0
