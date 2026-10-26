"""Spec 16.1 - transaction interpretation and logging (Claude mocked, plus the strict fallback)."""

from krona.claude import InterpreterUnavailable
from tests.conftest import item, tx


def only_tx(h):
    rows = h.conn.execute("SELECT * FROM transactions WHERE status = 'active'").fetchall()
    assert len(rows) == 1
    return rows[0], h.conn.execute("SELECT * FROM transaction_items WHERE transaction_id = ?", (rows[0]["id"],)).fetchall()


def test_single_expense_fallback(h):
    reply = h.say("Spent $12 on lunch")
    assert reply == "Logged.\nLunch, 1 purchase, $12.00"
    t, items = only_tx(h)
    assert (t["type"], t["total_cents"], t["description"]) == ("expense", 1200, "Lunch")
    assert h.store.category(t["category_id"])["name"] == "Food and groceries"


def test_single_expense_claude(hc):
    hc.interpreter.push(tx("expense", [item("Lunch", 1, None, "12")], category="Food and groceries"))
    assert hc.say("Spent $12 on lunch") == "Logged.\nLunch, 1 purchase, $12.00"


def test_multiple_identical_items_claude(hc):
    hc.interpreter.push(tx("expense", [item("Meiji protein milk", 2, "3.80", None, "bottle", "bottles")],
                           category="Food and groceries"))
    reply = hc.say("Bought 2 Meiji protein milks at $3.80 each")
    assert reply == "Logged.\nMeiji protein milk, 2 bottles, $7.60"
    t, items = only_tx(hc)
    assert t["total_cents"] == 760
    assert (items[0]["quantity"], items[0]["unit_price_cents"], items[0]["line_total_cents"]) == (2, 380, 760)


def test_multiple_identical_items_fallback(h):
    assert h.say("Bought 2 bottles of protein milk at $3.80 each") == "Logged.\nProtein milk, 2 bottles, $7.60"


def test_multiple_distinct_items(hc):
    hc.interpreter.push(tx("expense", [item("Coffee", 2, "2.50"), item("Croissant", 1, None, "4.20")],
                           category="Food and groceries"))
    reply = hc.say("2 coffees at $2.50 each and a croissant for $4.20")
    assert reply == "Logged.\nCoffee, 2 items, $5.00\nCroissant, 1 purchase, $4.20\nTotal: $9.20"
    t, items = only_tx(hc)
    assert t["total_cents"] == 920 and len(items) == 2


def test_variable_income(h):
    assert h.say("Received $900 internship pay") == "Logged.\nInternship pay, income, $900.00"
    t, _ = only_tx(h)
    assert (t["type"], t["total_cents"], t["income_kind"], t["category_id"]) == ("income", 90000, "salary", None)


def test_bonus_income(h):
    assert h.say("Got $60 bonus from my shift") == "Logged.\nBonus from my shift, income, $60.00"
    t, _ = only_tx(h)
    assert t["income_kind"] == "bonus"


def test_refund_linked_to_original(h):
    h.say("Bought shoes for $120")
    reply = h.say("Received a $20 refund for shoes")
    assert reply.startswith("Logged.\nShoes, refund, $20.00\nLinked to #1 (Shoes, Shopping).")
    r = h.conn.execute("SELECT * FROM transactions WHERE type = 'refund'").fetchone()
    assert r["related_transaction_id"] == 1


def test_refund_exceeding_original_asks(h):
    h.say("Bought shoes for $20")
    assert "still refundable" in h.say("Received a $50 refund for shoes")
    assert h.conn.execute("SELECT COUNT(*) n FROM transactions WHERE type='refund'").fetchone()["n"] == 0


def test_reversal(h):
    h.say("Spent $12 on lunch")
    assert "Reply yes or no" in h.say("/undo")
    assert h.say("yes").startswith("Reversed. #1")
    assert h.tx_count("active") == 0 and h.tx_count("reversed") == 1


def test_transfer_requires_accounts_and_is_not_income(h):
    assert h.say("Transferred $100 from my main account to savings").startswith("Not saved.")
    assert h.tx_count() == 0
    h.say("/accounts add Main main 1000")
    h.say("/accounts add Savings savings 0")
    assert h.say("Transferred $100 from my main account to savings") == "Logged.\nMain → Savings, transfer, $100.00"
    t, _ = only_tx(h)
    assert t["type"] == "transfer" and t["category_id"] is None


def test_missing_amount_asks_once_then_logs(h):
    assert h.say("Spent on lunch") == "What was the total amount paid?"
    assert h.tx_count() == 0
    assert h.say("$12") == "Logged.\nLunch, 1 purchase, $12.00"


def test_missing_amount_claude_clarify(hc):
    hc.interpreter.push({"intent": "clarify", "transaction": None, "purchase": None, "query": None,
                         "clarification_question": "What was the total amount paid?", "reply": None})
    assert hc.say("paid for parking") == "What was the total amount paid?"
    hc.interpreter.push(tx("expense", [item("Parking", 1, None, "3.50")], category="Transport"))
    assert hc.say("3.50") == "Logged.\nParking, 1 purchase, $3.50"
    # Claude saw the original message and the question it was answering.
    assert "paid for parking" in hc.interpreter.calls[-1][0]
    assert hc.interpreter.calls[-1][1]["earlier_clarification_question"] == "What was the total amount paid?"


def test_clarification_can_be_cancelled(h):
    h.say("Spent on lunch")
    assert h.say("cancel") == "Cancelled. Nothing was logged."
    assert h.tx_count() == 0


def test_ambiguous_message_not_logged(h):
    reply = h.say("lunch was expensive")
    assert "Nothing was logged" in reply and h.tx_count() == 0


def test_conflicting_total_asks(hc):
    hc.interpreter.push(tx("expense", [item("Coffee", 2, "2.50")], stated_total="6"))
    reply = hc.say("2 coffees at $2.50 each, $6 total")
    assert "add up to $5.00" in reply and "$6.00" in reply
    assert hc.tx_count() == 0


def test_conflicting_line_total_asks(hc):
    hc.interpreter.push(tx("expense", [item("Coffee", 2, "2.50", "6")]))
    assert "What was the total amount paid?" in hc.say("2 coffees at 2.50 each for 6")
    assert hc.tx_count() == 0


def test_explicit_past_date(h):
    assert h.say("I spent $18.50 on dinner yesterday") == "Logged.\nDinner, 1 purchase, $18.50\nDated 9 Oct."
    t, _ = only_tx(h)
    assert t["effective_date"] == "2026-10-09"


def test_explicit_date_from_claude_and_future_date_rejected(hc):
    hc.interpreter.push(tx("expense", [item("Taxi", 1, None, "15")], date="2026-10-05", category="Transport"))
    assert hc.say("Taxi $15 on 5 Oct").endswith("Dated 5 Oct.")
    hc.interpreter.push(tx("expense", [item("Taxi", 1, None, "15")], date="2026-10-20"))
    assert "future" in hc.say("Taxi $15 on 20 Oct")
    assert hc.tx_count() == 1


def test_hypothetical_purchase_not_logged_fallback(h):
    for msg in ("I might spend $40 on dinner tonight", "If I spend $100 on shoes will I be ok",
                "I'm going to buy a $300 phone"):
        h.say(msg)
    assert h.tx_count() == 0


def test_hypothetical_purchase_not_logged_claude(hc):
    hc.interpreter.push({"intent": "other", "transaction": None, "purchase": None, "query": None,
                         "clarification_question": None, "reply": "That is a plan, not a purchase. Nothing logged."})
    assert hc.say("I'm planning to spend $200 on a jacket") == "That is a plan, not a purchase. Nothing logged."
    assert hc.tx_count() == 0


def test_malformed_claude_output_never_logs(hc):
    for bad in ({"intent": "transaction"}, {"intent": "bogus"}, ["not", "a", "dict"],
                tx("expense", [{"description": "x", "quantity": "two", "unit_singular": None, "unit_plural": None,
                                "unit_price": None, "line_total": "5"}])):
        hc.interpreter.push(bad)
        reply = hc.say("buy something weird for 5")
        assert not reply.startswith("Logged.")
    assert hc.tx_count() == 0


def test_malformed_output_falls_back_to_strict_parser(hc):
    hc.interpreter.push({"intent": "transaction", "transaction": "garbage"})
    assert hc.say("Spent $12 on lunch") == "Logged.\nLunch, 1 purchase, $12.00"


def test_claude_invented_amount_rejected(hc):
    # Claude claims $15 but the user never said 15.
    hc.interpreter.push(tx("expense", [item("Lunch", 1, None, "15")]))
    assert hc.say("Had lunch at the hawker centre") == "What was the total amount paid?"
    assert hc.tx_count() == 0


def test_claude_invented_quantity_rejected(hc):
    hc.interpreter.push(tx("expense", [item("Coffee", 3, "2.50")]))
    assert "How many" in hc.say("Bought coffee at $2.50")
    assert hc.tx_count() == 0


def test_claude_timeout_fails_safely(hc):
    hc.interpreter.push(InterpreterUnavailable("Claude timed out"))
    reply = hc.say("grabbed a bite with friends, it was about thirty bucks")
    assert "Nothing was logged" in reply and "unavailable" in reply
    assert hc.tx_count() == 0


def test_claude_timeout_simple_message_still_logs_via_strict_parser(hc):
    hc.interpreter.push(InterpreterUnavailable("Claude timed out"))
    assert hc.say("Spent $25 on transport") == "Logged.\nTransport, 1 purchase, $25.00"


def test_currency_formats(h):
    h.say("Spent S$12 on lunch")
    h.say("Paid SGD 25 for transport")
    totals = [r["total_cents"] for r in h.conn.execute("SELECT total_cents FROM transactions ORDER BY id")]
    assert totals == [1200, 2500]


def test_uncategorised_is_explicit(h):
    reply = h.say("Spent $9 on widgets")
    assert "Category: Uncategorised. Set it with /edit 1 category <name>." in reply


def test_correction_via_edit(h):
    h.say("Spent $12 on lunch")
    reply = h.say("/edit 1 amount 15.20")
    assert reply == "Corrected. #2 replaces #1.\nLunch, 1 purchase, $15.20"
    assert h.tx_count("active") == 1 and h.tx_count("corrected") == 1


def test_hypothetical_backstop_even_if_claude_says_transaction(hc):
    for msg in ("I might spend $40 on dinner tonight", "Should I spend $40 on dinner?",
                "Thinking of buying shoes for $120", "If I spend $100 on shoes, what happens"):
        hc.interpreter.push(tx("expense", [item("Dinner", 1, None, "40")]))
        assert "nothing was logged" in hc.say(msg)
    assert hc.tx_count() == 0


def test_model_prose_cannot_introduce_figures(hc):
    hc.interpreter.push({"intent": "other", "transaction": None, "purchase": None, "query": None,
                         "clarification_question": None, "reply": "You have $500 left this month."})
    assert "$500" not in hc.say("hello")
    hc.interpreter.push({"intent": "clarify", "transaction": None, "purchase": None, "query": None,
                         "clarification_question": "Was it $14?", "reply": None})
    assert hc.say("had lunch") == "What was the total amount paid?"


def test_fallback_price_answer_for_purchase_question(h):
    h.say("/budget overall 1000")
    assert h.say("should i buy these shoes?") == "What is the price?"
    assert h.say("$120").splitlines()[0] in ("BUY", "WAIT", "LET IT GO")
    assert h.tx_count() == 0


def test_currency_prefix_without_space(h):
    assert h.say("Paid SGD12 for lunch") == "Logged.\nLunch, 1 purchase, $12.00"
    assert h.say("Spent S$7.50 on kopi") == "Logged.\nKopi, 1 purchase, $7.50"
