"""Telegram interface helpers: reply keyboards, the command menu, short help, and plain-word shortcuts.

Buttons are Telegram reply keyboards: tapping one simply sends its label as a message, so every button
goes through the same routing (and idempotency) as typed text.
"""

from __future__ import annotations

import re

BTN_TODAY, BTN_REPORT, BTN_BUDGET = "Today", "Report", "Budget"
BTN_HISTORY, BTN_UNDO, BTN_HELP = "History", "Undo", "Help"

MAIN_KEYBOARD = {
    "keyboard": [[BTN_TODAY, BTN_REPORT, BTN_BUDGET], [BTN_HISTORY, BTN_UNDO, BTN_HELP]],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "e.g. lunch 12",
}
YES_NO_KEYBOARD = {"keyboard": [["Yes", "No"]], "resize_keyboard": True, "one_time_keyboard": True}


def choice_keyboard(n: int) -> dict:
    nums = [str(i) for i in range(1, n + 1)]
    return {"keyboard": [nums, ["Cancel"]], "resize_keyboard": True, "one_time_keyboard": True}


# Shown in Telegram's "/" menu (registered with setMyCommands at startup).
COMMAND_MENU = [
    ("today", "What I spent today and what's left"),
    ("report", "Full review of this month"),
    ("budget", "Limits and how much is left"),
    ("history", "Recent entries"),
    ("undo", "Undo the last entry"),
    ("edit", "Fix an entry"),
    ("accounts", "Account balances (optional)"),
    ("goals", "Savings goals (optional)"),
    ("help", "How to use KRONA"),
]

HELP = """How to use KRONA

Log spending: just type it
  lunch 12
  coffee $4.50
  spent $30 on groceries yesterday
Log income: received $900 pay
Ask first: should i buy shoes for $120?

Buttons: Today · Report · Budget · History · Undo · Help

Set limits
  food budget 400
  overall budget 1500

Fix a mistake
  Undo removes the last entry
  /edit 15.20 changes the last entry's amount
  /edit lunch 15.20 changes the last "lunch" entry

More help: /help budget · /help accounts · /help goals · /help edit"""

HELP_TOPICS = {
    "budget": """Budgets
food budget 400 (or /budget food 400): monthly limit for a category
overall budget 1500: limit for all spending in a month
/budget food off: remove a category limit
/budget: see what's left

Bills you know are coming
/budget commit add Rent 800 day 1: every month on the 1st
/budget commit add Course 300 on 2026-11-15: one-off
/budget commit list · commit paid Rent · commit remove Rent

Other
/budget payday 2026-11-10: when your next income is expected
/budget cooloff 100 48: wait 48 hours before buys of $100+
/budget categories: list categories
/budget category add Pets · rename Pets to Pet care
/budget category disable Travel · move Travel to Shopping""",
    "accounts": """Accounts (optional; balances are what you enter, not live bank data)
/accounts: show balances
/accounts add DBS 2500: add an account with its current balance
/accounts add Savings 1000 savings: give the type (main, spending, savings, investment, cash)
/accounts reconcile DBS 2410.50: correct a balance to match your bank
/accounts remove DBS
Then: "transferred $100 from DBS to Savings\"""",
    "goals": """Savings goals (optional)
/goals: show progress
/goals add Emergency 5000: a target
/goals add Emergency 5000 monthly 300 account Savings: with a monthly amount and linked account
/goals protect 2000: money KRONA never counts as spendable
/goals remove Emergency""",
    "edit": """Fixing entries
Undo (button) or /undo: remove the last entry (asks first)
/undo lunch: pick from entries matching "lunch"
/edit 15.20: change the last entry's amount
/edit lunch 15.20: change the last "lunch" entry's amount
/edit 3 15.20: change entry #3 (numbers are shown in History)
/edit 3 category Transport · /edit 3 date yesterday · /edit 3 description Dinner
/edit 3 quantity 2 · /edit 3 price 3.80
Nothing is ever deleted: corrections are kept in the audit trail.""",
}

# Plain words (and button labels) that mean a command.
KEYWORDS = {
    "today": "today", "report": "report", "budget": "budget", "budgets": "budget", "limits": "budget",
    "history": "history", "recent": "history", "undo": "undo", "help": "help", "menu": "start",
    "start": "start", "hi": "start", "hello": "start", "hey": "start", "accounts": "accounts",
    "balance": "accounts", "balances": "accounts", "goals": "goals", "?": "help", "commands": "help",
}

_UNDO_PHRASES = re.compile(r"^(undo( that| it| last( one| entry)?)?|delete (the )?last( one| entry)?|"
                           r"remove (the )?last( one| entry)?|that was (a )?mistake|oops)$")
_TODAY_PHRASES = re.compile(r"^(what|how much) (did|have) i (spend|spent)( so far)? today$|"
                            r"^(spent|spending) today$|^today'?s spending$")
_MONTH_PHRASES = re.compile(r"^(what|how much) (did|have) i (spend|spent)( so far)? this month$|"
                            r"^(spent|spending) this month$")
_HOWTO = re.compile(r"^(how (do|can) i|how to|what can you do|what do i do)\b")


def keyword_command(text: str) -> str | None:
    """Map a plain message ('help', 'undo', a button tap) to a command name, or None."""
    if text.strip() == "?":
        return "help"
    t = " ".join(text.lower().strip(" .!?").split())
    if t in KEYWORDS:
        return KEYWORDS[t]
    if _UNDO_PHRASES.match(t):
        return "undo"
    if _TODAY_PHRASES.match(t):
        return "today"
    if _MONTH_PHRASES.match(t):
        return "month"
    if _HOWTO.match(t):
        for words, topic in (("budget|limit|bill|payday", "budget"), ("account|balance|transfer", "accounts"),
                             ("goal|sav", "goals"), ("edit|fix|change|categor|undo|delete|wrong|mistake", "edit")):
            if re.search(words, t):
                return f"help {topic}"
        return "help"
    return None


_AMOUNT = r"(?:s\$\s?|sgd\s?|\$)?(\d[\d,]*(?:\.\d{1,2})?|off|none|clear|remove)"
_BUDGET_PHRASES = [
    re.compile(r"^(?:set\s+)?(?:my\s+|the\s+|a\s+)?(.+?)\s+(?:budget|limit)\s+(?:to\s+|at\s+|of\s+|is\s+|=\s*)?"
               + _AMOUNT + r"(?:\s+(?:a|per)\s+month)?$"),
    re.compile(r"^(?:budget|limit)\s+(?:for\s+)?(.+?)\s+(?:to\s+|at\s+|=\s*)?" + _AMOUNT + r"$"),
    re.compile(r"^(?:set\s+)?(?:a\s+)?(?:budget|limit)\s+of\s+(?:s\$\s?|sgd\s?|\$)?(?P<amt>\d[\d,]*(?:\.\d{1,2})?)"
               r"\s+for\s+(?P<cat>.+)$"),
]
OVERALL_WORDS = {"overall", "total", "monthly", "overall spending", "total spending", "spending", "month",
                 "my monthly", "overall monthly", "whole", "all"}


def budget_phrase(text: str) -> tuple[str, str] | None:
    """'food budget 400' / 'set my food budget to 400' -> ('food', '400'); category 'overall' for totals."""
    t = " ".join(text.lower().strip(" .!").split())
    for i, rx in enumerate(_BUDGET_PHRASES):
        m = rx.match(t)
        if not m:
            continue
        if i == 2:
            cat, amt = m.group("cat"), m.group("amt")
        else:
            cat, amt = m.group(1), m.group(2)
        cat = re.sub(r"\s+(?:monthly|month)$", "", cat).strip()
        return ("overall" if cat in OVERALL_WORDS else cat), amt
    return None
