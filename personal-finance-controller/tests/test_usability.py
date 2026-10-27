"""Usability fixes from the 27 Oct diagnostic: shorthand, plain words, buttons, forgiving commands."""

import pytest

from krona.ui import MAIN_KEYBOARD, YES_NO_KEYBOARD


@pytest.mark.parametrize("msg,expected", [
    ("lunch 12", "Logged.\nLunch, 1 purchase, $12.00"),
    ("coffee $4.50", "Logged.\nCoffee, 1 purchase, $4.50"),
    ("$12 lunch", "Logged.\nLunch, 1 purchase, $12.00"),
    ("chicken rice 4.50", "Logged.\nChicken rice, 1 purchase, $4.50"),
    ("grab 15 yesterday", "Logged.\nGrab, 1 purchase, $15.00\nDated 9 Oct."),
])
def test_shorthand_logging(h, msg, expected):
    assert h.say(msg) == expected


@pytest.mark.parametrize("msg", ["salary 900", "refund 20", "budget 400x", "should i buy shoes 120?", "12", "lunch"])
def test_shorthand_does_not_guess_income_refunds_or_questions(h, msg):
    h.say(msg)
    assert h.tx_count() == 0


def test_shorthand_category_guess(h):
    h.say("grab 15")
    t = h.store.recent(1)[0]
    assert h.store.category(t["category_id"])["name"] == "Transport"


@pytest.mark.parametrize("word,marker", [
    ("help", "How to use KRONA"), ("Help", "How to use KRONA"), ("?", "How to use KRONA"),
    ("hi", "Every dollar, accounted for."), ("menu", "Every dollar, accounted for."),
    ("Report", "KRONA REPORT"), ("budget", "BUDGET ·"), ("History", "Nothing logged yet"),
    ("Today", "TODAY · 10 Oct"), ("what did i spend today", "TODAY · 10 Oct"),
    ("how do I change a category", "Fixing entries"), ("how do i set a budget", "Budgets"),
])
def test_plain_words_and_buttons_route_to_commands(h, word, marker):
    assert marker in h.say(word)
    assert h.tx_count() == 0


def test_today_shows_spending_not_just_allowance(h):
    h.say("/budget overall 660")
    h.say("lunch 12")
    h.say("coffee 3")
    r = h.say("what did i spend today")
    assert "Spent $15.00: Lunch $12.00, Coffee $3.00" in r and "FREE TO SPEND" in r


def test_month_spending_question(h):
    h.say("lunch 12")
    assert "Food and groceries: $12.00" in h.say("how much did I spend this month")


@pytest.mark.parametrize("msg,cat,cents", [
    ("food budget 400", "Food and groceries", 40000),
    ("set my food budget to 400", "Food and groceries", 40000),
    ("Set transport limit to $120", "Transport", 12000),
    ("budget for shopping 200", "Shopping", 20000),
    ("set a budget of $50 for gifts", "Gifts and miscellaneous", 5000),
    ("/budget food 400", "Food and groceries", 40000),
])
def test_budget_in_plain_words(h, msg, cat, cents):
    h.say(msg)
    assert h.store.active_limits()[h.store.resolve_category(cat)["id"]] == cents


@pytest.mark.parametrize("msg", ["overall budget 1500", "monthly budget 1500", "/budget 1500", "set my total budget to 1500"])
def test_overall_budget_in_plain_words(h, msg):
    assert h.say(msg).startswith("Monthly spending limit set to $1,500.00.")
    assert h.store.active_limits()[None] == 150000


def test_budget_off_and_bad_input(h):
    h.say("food budget 400")
    assert h.say("/budget food off") == "Food and groceries limit removed."
    assert "Try: /budget food 400" in h.say("/budget food lots")


def test_undo_in_plain_words_with_yes_no_buttons(h):
    h.say("lunch 12")
    assert h.say("delete the last one") == "Undo this entry?\nLunch $12.00 · 10 Oct"
    assert h.sender.keyboards[-1] == YES_NO_KEYBOARD
    assert h.say("Yes").startswith("Undone:")
    assert h.sender.keyboards[-1] == MAIN_KEYBOARD
    assert h.tx_count("active") == 0


def test_undo_no_keeps_entry(h):
    h.say("lunch 12")
    h.say("undo")
    assert h.say("No") == "OK, kept it."
    assert h.tx_count("active") == 1


def test_choice_buttons_for_several_matches(h):
    h.say("lunch 12")
    h.say("lunch with team 30")
    h.say("/undo lunch")
    assert h.sender.keyboards[-1]["keyboard"] == [["1", "2"], ["Cancel"]]
    assert h.say("Cancel") == "OK, nothing changed."


def test_duplicate_prompt_has_yes_no_buttons(h):
    h.say("coffee 3")
    assert h.say("coffee 3") == "You logged Coffee $3.00 a moment ago.\nLog it again as a second purchase?"
    assert h.sender.keyboards[-1] == YES_NO_KEYBOARD
    h.say("Yes")
    assert h.tx_count() == 2


def test_tapping_a_button_abandons_a_question(h):
    assert h.say("spent on lunch") == "What was the total amount paid?"
    assert "KRONA REPORT" in h.say("Report")
    assert h.store.get_pending() is None


def test_every_normal_reply_carries_the_main_menu(h):
    for msg in ("/start", "lunch 12", "/report", "/budget"):
        h.say(msg)
        assert h.sender.keyboards[-1] == MAIN_KEYBOARD


@pytest.mark.parametrize("args,expected_total", [
    ("/edit 15.20", 1520), ("/edit lunch 15.20", 1520), ("/edit 1 15.20", 1520), ("/edit 1 amount 15.20", 1520),
])
def test_edit_forms(h, args, expected_total):
    h.say("lunch 12")
    assert h.say(args) == f"Fixed.\nLunch, 1 purchase, ${expected_total // 100}.{expected_total % 100:02d}"
    assert h.store.recent(1)[0]["total_cents"] == expected_total


def test_edit_by_name_targets_latest_match_and_any_field(h):
    h.say("lunch 12")
    h.say("coffee 3")
    h.say("/edit lunch category transport")
    lunch = [r for r in h.store.recent(5) if r["description"] == "Lunch"][0]
    assert h.store.category(lunch["category_id"])["name"] == "Transport"
    assert "Nothing matches 'dinner'" in h.say("/edit dinner 5")


def test_accounts_add_without_type(h):
    assert h.say("/accounts add DBS 2500").startswith("Added DBS (main) with $2,500.00.")
    assert h.say("/accounts add My Savings 1000").startswith("Added My Savings (savings)")
    assert h.say("/accounts add Wallet 50 cash").startswith("Added Wallet (cash)")
    assert "/accounts add DBS 2500" in h.say("/accounts add")


def test_unknown_message_is_short_and_helpful(h):
    r = h.say("asdf qwerty")
    assert r.startswith("I didn't understand that, so nothing was logged.") and len(r) < 150


def test_help_is_short(h):
    assert len(h.say("/help")) < 600
    assert len(h.say("/start")) < 400
