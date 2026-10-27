"""Strict, deterministic fallback interpreter used only when Claude is unavailable.

It recognises a narrow set of unambiguous phrasings and returns the same raw intent shape as Claude.
Anything else returns None, and the bot asks the user to retry instead of guessing.
"""

from __future__ import annotations

import re
from datetime import date

from .timeutil import parse_date_arg

AMT = r"(?:s\$\s?|sgd\s?|\$)?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?:\s?(?:sgd|dollars?))?"
CUR_AMT = r"(?:s\$\s?|sgd\s?|\$)((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)"
DATE = r"(?:\s+(yesterday|today|on\s+\d{4}-\d{1,2}-\d{1,2}|on\s+\d{1,2}\s+[a-z]{3,9}))?"
NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "dozen": 12, "a dozen": 12}
QTY = r"(\d+|a dozen|" + "|".join(k for k in NUM_WORDS if k != "a dozen") + r")"

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Food and groceries": ("lunch", "dinner", "breakfast", "brunch", "supper", "meal", "food", "groceries",
                           "grocery", "coffee", "kopi", "teh", "milk", "snack", "snacks", "bubble tea", "hawker",
                           "restaurant", "drink", "bread", "fruit", "noodles", "rice"),
    "Transport": ("transport", "grab", "taxi", "cab", "mrt", "bus", "train", "fare", "ez-link", "ezlink",
                  "gojek", "petrol", "parking"),
    "Bills and subscriptions": ("bill", "bills", "phone bill", "subscription", "netflix", "spotify",
                                "electricity", "utilities", "rent", "internet", "insurance"),
    "Health and personal care": ("doctor", "clinic", "medicine", "pharmacy", "haircut", "gym", "dental",
                                 "dentist", "shampoo", "toiletries"),
    "Shopping": ("shoes", "clothes", "shirt", "t-shirt", "jeans", "bag", "shopee", "lazada", "amazon"),
    "Entertainment and social": ("movie", "movies", "cinema", "game", "games", "concert", "party", "bar",
                                 "karaoke"),
    "Education and career": ("book", "books", "course", "textbook", "tuition", "class", "exam"),
    "Travel": ("flight", "hotel", "airbnb", "hostel"),
    "Gifts and miscellaneous": ("gift", "present", "donation"),
}

_QUESTION_START = re.compile(
    r"^(should|can|could|would|is|am|are|do|does|did|what|how|why|when|where|which|if|maybe|might|will|"
    r"i might|i may|i will|i'll|i want|i'd|i would|i'm going|im going|i am going|thinking|planning|"
    r"compare|show|tell)\b"
)


_NOT_SHORTHAND = re.compile(
    r"\b(spent|spend|paid|pay|bought|buy|received|receive|got|earned|transferred|transfer|moved|salary|income|"
    r"wage|wages|allowance|bonus|refund|refunded|budget|limit|goal|goals|account|accounts|edit|undo|report|"
    r"history|help|owe|owes|lent|borrowed)\b")


def _shorthand_ok(desc: str) -> bool:
    """A shorthand description must look like a purchase, not income, a refund, a command or a question."""
    d = desc.strip()
    return bool(d) and len(d.split()) <= 5 and not _NOT_SHORTHAND.search(d) and not _QUESTION_START.match(d)


def guess_category(desc: str) -> str | None:
    words = desc.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            if re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", words):
                return cat
    return None


def _empty_tx(ttype: str) -> dict:
    return {"type": ttype, "items": [], "stated_total": None, "category": None, "date": None, "account": None,
            "from_account": None, "to_account": None, "income_kind": None, "refund_of": None}


def _result(intent: str, **kw) -> dict:
    base = {"intent": intent, "transaction": None, "purchase": None, "query": None,
            "clarification_question": None, "reply": None}
    base.update(kw)
    return base


def _date(token: str | None, today: date) -> str | None:
    if not token:
        return None
    t = token.strip()
    if t.startswith("on "):
        t = t[3:]
    d = parse_date_arg(t, today)
    return d.isoformat() if d else None


def _cap(s: str) -> str:
    s = s.strip(" .,!")
    return s[:1].upper() + s[1:]


def _clean_desc(s: str) -> str:
    s = re.sub(r"^(?:my|a|an|the|some)\s+", "", s.strip())
    return _cap(s)


def _split_unit(desc: str, qty: int) -> tuple[str, str | None, str | None]:
    m = re.match(r"^(bottles?|cups?|packs?|boxes|box|cans?|bags?|pieces?|tickets?|slices?|bowls?)\s+of\s+(.+)$",
                 desc, re.I)
    if not m:
        return desc, None, None
    unit = m.group(1).lower()
    singular = {"boxes": "box"}.get(unit, unit[:-1] if unit.endswith("s") else unit)
    plural = {"box": "boxes"}.get(singular, singular + "s")
    return m.group(2), singular, plural


def _income_kind(desc: str) -> str:
    d = desc.lower()
    if "bonus" in d:
        return "bonus"
    if "allowance" in d:
        return "allowance"
    if re.search(r"\b(pay|salary|wage|wages|payslip|paycheque|paycheck)\b", d):
        return "salary"
    return "other"


def _recurring(t: str) -> str:
    if re.search(r"(a|per|/|every)\s?(month|mo)\b|monthly", t):
        return "monthly"
    if re.search(r"(a|per|/|every)\s?(week|wk)\b|weekly", t):
        return "weekly"
    if re.search(r"(a|per|/|every)\s?(year|yr)\b|yearly|annual", t):
        return "yearly"
    return "none"


def parse(text: str, today: date) -> dict | None:
    t = " ".join((text or "").strip().lower().split()).rstrip(".!")
    if not t:
        return None

    # Purchase questions are never transactions.
    m = re.match(r"^(?:should|can|could) i (?:buy|get|subscribe to|afford|purchase|order)\b(.*)$", t)
    if m:
        rest = m.group(1)
        price = re.search(CUR_AMT, rest) or re.search(r"\bfor\s+" + AMT, rest)
        item = re.sub(r"\s+(?:for|at)\s+" + AMT + r".*$", "", rest).strip(" ?")
        item = re.sub(r"^(?:this|these|that|those|a|an|the)\s+", "", item) or "this"
        return _result("purchase_question", purchase={
            "item": _cap(item), "price": price.group(1) if price else None, "recurring": _recurring(rest),
            "category": guess_category(item), "need": "unknown",
            "asks_timing": bool(re.search(r"\b(wait|payday|later|now)\b", rest)),
        })

    if t.endswith("?") or _QUESTION_START.match(t):
        return None  # questions and hypotheticals: leave to Claude, never log

    # Refund: "received a $20 refund for shoes"
    m = re.match(r"^(?:i\s+)?(?:received|got)\s+(?:a\s+|an\s+)?" + AMT + r"\s+refund(?:\s+(?:for|on|from)\s+(.+?))?"
                 + DATE + r"$", t)
    if m:
        tx = _empty_tx("refund")
        what = _clean_desc(m.group(2)) if m.group(2) else "Refund"
        tx["items"] = [{"description": what, "quantity": 1, "unit_singular": None, "unit_plural": None,
                        "unit_price": None, "line_total": m.group(1)}]
        tx["refund_of"] = m.group(2)
        tx["category"] = guess_category(m.group(2) or "")
        tx["date"] = _date(m.group(3), today)
        return _result("transaction", transaction=tx)

    # Transfer: "transferred $100 from my main account to savings"
    m = re.match(r"^(?:i\s+)?(?:transferred|moved)\s+" + AMT + r"\s+from\s+(.+?)\s+to\s+(.+?)" + DATE + r"$", t)
    if m:
        tx = _empty_tx("transfer")
        tx["items"] = [{"description": "Transfer", "quantity": 1, "unit_singular": None, "unit_plural": None,
                        "unit_price": None, "line_total": m.group(1)}]
        tx["from_account"], tx["to_account"] = m.group(2), m.group(3)
        tx["date"] = _date(m.group(4), today)
        return _result("transaction", transaction=tx)

    # Income: "received $900 internship pay", "got $60 bonus from my shift"
    m = re.match(r"^(?:i\s+)?(?:received|got|earned)\s+(?:a\s+|an\s+|my\s+)?" + CUR_AMT + r"\s+(.+?)" + DATE + r"$", t)
    if m and not re.search(r"\b(refund|change|discount|voucher)\b", m.group(2)):
        desc = re.sub(r"^(?:as|in|of|for)\s+", "", m.group(2))
        tx = _empty_tx("income")
        tx["items"] = [{"description": _clean_desc(desc), "quantity": 1, "unit_singular": None,
                        "unit_plural": None, "unit_price": None, "line_total": m.group(1)}]
        tx["income_kind"] = _income_kind(desc)
        tx["date"] = _date(m.group(3), today)
        return _result("transaction", transaction=tx)

    # Expense with quantity and unit price: "bought 2 bottles of protein milk at $3.80 each"
    m = re.match(r"^(?:i\s+)?(?:bought|got|purchased|ordered)\s+" + QTY + r"\s+(.+?)\s+(?:at|for|@)\s+" + AMT
                 + r"\s+(?:each|apiece|per\s+\w+)" + DATE + r"$", t)
    if m:
        qty = int(m.group(1)) if m.group(1).isdigit() else NUM_WORDS[m.group(1)]
        desc, us, up = _split_unit(m.group(2), qty)
        tx = _empty_tx("expense")
        tx["items"] = [{"description": _clean_desc(desc), "quantity": qty, "unit_singular": us, "unit_plural": up,
                        "unit_price": m.group(3), "line_total": None}]
        tx["category"] = guess_category(desc)
        tx["date"] = _date(m.group(4), today)
        return _result("transaction", transaction=tx)

    # Expense, single amount: "spent $12 on lunch", "paid $25 for transport", "i spent $18.50 on dinner yesterday"
    m = re.match(r"^(?:i\s+)?(?:spent|paid)\s+" + AMT + r"\s+(?:on|for)\s+(.+?)" + DATE + r"$", t)
    if m:
        desc = m.group(2)
        tx = _empty_tx("expense")
        tx["items"] = [{"description": _clean_desc(desc), "quantity": 1, "unit_singular": None, "unit_plural": None,
                        "unit_price": None, "line_total": m.group(1)}]
        tx["category"] = guess_category(desc)
        tx["date"] = _date(m.group(3), today)
        return _result("transaction", transaction=tx)

    # Expense: "bought shoes for $120"
    m = re.match(r"^(?:i\s+)?(?:bought|got|purchased|ordered)\s+(.+?)\s+for\s+" + AMT + DATE + r"$", t)
    if m:
        tx = _empty_tx("expense")
        tx["items"] = [{"description": _clean_desc(m.group(1)), "quantity": 1, "unit_singular": None,
                        "unit_plural": None, "unit_price": None, "line_total": m.group(2)}]
        tx["category"] = guess_category(m.group(1))
        tx["date"] = _date(m.group(3), today)
        return _result("transaction", transaction=tx)

    # Shorthand people actually type: "lunch 12", "coffee $4.50", "grab 15 yesterday", "$12 lunch"
    desc = amount = when = None
    m = re.match(r"^([a-z][a-z'&\- ]{0,40}?)\s+" + AMT + DATE + r"$", t)
    if m:
        desc, amount, when = m.group(1), m.group(2), m.group(3)
    else:
        m = re.match(r"^" + CUR_AMT + r"\s+(?:on\s+|for\s+)?([a-z][a-z'&\- ]{0,40}?)" + DATE + r"$", t)
        if m:
            amount, desc, when = m.group(1), m.group(2), m.group(3)
    if desc and _shorthand_ok(desc):
        tx = _empty_tx("expense")
        tx["items"] = [{"description": _clean_desc(desc), "quantity": 1, "unit_singular": None,
                        "unit_plural": None, "unit_price": None, "line_total": amount}]
        tx["category"] = guess_category(desc)
        tx["date"] = _date(when, today)
        return _result("transaction", transaction=tx)

    # Missing amount: "spent on lunch", "paid for parking"
    if re.match(r"^(?:i\s+)?(?:spent|paid|bought)\b", t) and not re.search(r"\d", t):
        return _result("clarify", clarification_question="What was the total amount paid?")
    return None


def parse_query(text: str) -> dict | None:
    """Keyword routing for finance questions when Claude is unavailable."""
    t = (text or "").lower()
    q = None
    if re.search(r"\b(safe(ly)? (to )?spend|can i spend|free to spend|spend today|allowance)\b", t):
        q = {"type": "safe_today", "category": None}
    elif re.search(r"\bcompare\b|last month|vs\.? last|previous month", t):
        q = {"type": "compare_months", "category": None}
    elif re.search(r"\bwhy\b.*\b(overspend|over budget|spending so much)", t):
        q = {"type": "why_overspending", "category": None}
    elif re.search(r"\b(show|how much).*\b(spend|spent|spending)\b", t):
        cat = None
        for c in CATEGORY_KEYWORDS:
            if c.split()[0].lower() in t:
                cat = c
        q = {"type": "category_spending", "category": cat}
    elif re.search(r"\breport\b", t):
        q = {"type": "report", "category": None}
    return _result("query", query=q) if q else None


_HYPOTHETICAL = re.compile(r"\b(if i|going to (buy|spend|get|pay)|planning to|plan to|thinking of|thinking about|"
                           r"should i|could i|can i afford|would it|might buy|might spend|may buy)\b")


def looks_hypothetical(text: str) -> bool:
    """True for questions and plans. Used as a backstop so they are never logged as transactions."""
    t = " ".join((text or "").strip().lower().split())
    if t.endswith("?") or _QUESTION_START.match(t):
        return True
    return bool(_HYPOTHETICAL.search(t))
