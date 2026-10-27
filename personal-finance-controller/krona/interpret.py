"""Validation of interpreter output and construction of ledger drafts.

Nothing produced by Claude (or the fallback parser) reaches the database without passing through
here. All arithmetic is done in integer cents by this module, never trusted from the model.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .money import MoneyError, fmt, numbers_in_text, parse_cents, to_decimal
from .parser import NUM_WORDS, guess_category
from .store import ItemDraft, Store, TxDraft

log = logging.getLogger("krona.interpret")

MAX_ITEMS = 20
MAX_QTY = 1000
MAX_AGE_DAYS = 400


class Malformed(ValueError):
    """Interpreter output does not match the expected structure."""


@dataclass
class Clarify:
    question: str
    from_model: bool = False  # True when the wording came from Claude rather than KRONA's own checks


@dataclass
class Reject:
    message: str


@dataclass
class Hypothetical:
    """A question or plan that an interpreter tried to treat as a transaction."""


@dataclass
class TxPlan:
    draft: TxDraft


@dataclass
class PurchaseQ:
    item: str
    price_cents: int | None
    recurring: str
    category_id: int | None
    need: str
    asks_timing: bool


@dataclass
class Query:
    type: str
    category_id: int | None
    category_text: str | None


@dataclass
class Other:
    reply: str | None


INTENTS = {"transaction", "purchase_question", "query", "clarify", "other"}
TX_TYPES = {"expense", "income", "refund", "transfer"}
QUERY_TYPES = {"report", "category_spending", "compare_months", "safe_today", "why_overspending", "history",
               "accounts", "budget"}


def _opt_str(v, field: str, max_len: int = 120) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise Malformed(f"{field} must be a string")
    v = " ".join(v.split())
    if len(v) > max_len:
        raise Malformed(f"{field} too long")
    return v or None


def validate_raw(raw) -> dict:
    """Structural validation. Raises Malformed on anything unexpected."""
    if not isinstance(raw, dict):
        raise Malformed("not an object")
    intent = raw.get("intent")
    if intent not in INTENTS:
        raise Malformed("unknown intent")
    out = {"intent": intent, "transaction": None, "purchase": None, "query": None,
           "clarification_question": _opt_str(raw.get("clarification_question"), "clarification_question", 300),
           "reply": _opt_str(raw.get("reply"), "reply", 600)}
    if intent == "transaction":
        tx = raw.get("transaction")
        if not isinstance(tx, dict):
            raise Malformed("transaction missing")
        if tx.get("type") not in TX_TYPES:
            raise Malformed("bad transaction type")
        items = tx.get("items")
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            raise Malformed("items must be a list")
        clean_items = []
        for it in items:
            if not isinstance(it, dict):
                raise Malformed("item must be an object")
            q = it.get("quantity")
            if q is not None and (isinstance(q, bool) or not isinstance(q, int)):
                raise Malformed("quantity must be an integer")
            clean_items.append({
                "description": _opt_str(it.get("description"), "description", 80),
                "quantity": q,
                "unit_singular": _opt_str(it.get("unit_singular"), "unit", 20),
                "unit_plural": _opt_str(it.get("unit_plural"), "unit", 20),
                "unit_price": _opt_str(it.get("unit_price"), "unit_price", 20),
                "line_total": _opt_str(it.get("line_total"), "line_total", 20),
            })
        d = tx.get("date")
        if d is not None:
            if not isinstance(d, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                raise Malformed("bad date")
        ik = tx.get("income_kind")
        if ik not in (None, "salary", "bonus", "allowance", "other"):
            raise Malformed("bad income kind")
        out["transaction"] = {
            "type": tx["type"], "items": clean_items,
            "stated_total": _opt_str(tx.get("stated_total"), "stated_total", 20),
            "category": _opt_str(tx.get("category"), "category", 60), "date": d,
            "account": _opt_str(tx.get("account"), "account", 60),
            "from_account": _opt_str(tx.get("from_account"), "from_account", 60),
            "to_account": _opt_str(tx.get("to_account"), "to_account", 60),
            "income_kind": ik, "refund_of": _opt_str(tx.get("refund_of"), "refund_of", 80),
        }
    elif intent == "purchase_question":
        p = raw.get("purchase")
        if not isinstance(p, dict):
            raise Malformed("purchase missing")
        if p.get("recurring") not in ("none", "weekly", "monthly", "yearly"):
            raise Malformed("bad recurring")
        if p.get("need") not in ("need", "want", "unknown"):
            raise Malformed("bad need")
        out["purchase"] = {
            "item": _opt_str(p.get("item"), "item", 80) or "This purchase",
            "price": _opt_str(p.get("price"), "price", 20), "recurring": p["recurring"],
            "category": _opt_str(p.get("category"), "category", 60), "need": p["need"],
            "asks_timing": bool(p.get("asks_timing")),
        }
    elif intent == "query":
        q = raw.get("query")
        if not isinstance(q, dict) or q.get("type") not in QUERY_TYPES:
            raise Malformed("bad query")
        out["query"] = {"type": q["type"], "category": _opt_str(q.get("category"), "category", 60)}
    elif intent == "clarify" and not out["clarification_question"]:
        raise Malformed("clarify without question")
    return out


# --------------------------------------------------------------------------- helpers
def _stated_in_text(value: str, text: str) -> bool:
    try:
        return to_decimal(value).normalize() in numbers_in_text(text)
    except MoneyError:
        return False


def _quantity_in_text(q: int, text: str) -> bool:
    if q == 1:
        return True
    t = text.lower()
    if Decimal(q) in numbers_in_text(t):
        return True
    return any(n == q and re.search(r"\b" + re.escape(w) + r"\b", t) for w, n in NUM_WORDS.items())


def _cents(value: str | None, text: str, what: str) -> int | None | Clarify:
    if value is None:
        return None
    try:
        c = parse_cents(value)
    except MoneyError:
        return Clarify(f"What was the {what}?")
    if not _stated_in_text(value, text):
        log.warning("interpreter produced an amount not present in the message; rejected")
        return Clarify("What was the total amount paid?")
    return c


def resolve_date(raw_date: str | None, text: str, today: date) -> date | Clarify:
    t = text.lower()
    if re.search(r"\bday before yesterday\b", t):
        d = today - timedelta(days=2)
    elif re.search(r"\byesterday\b", t):
        d = today - timedelta(days=1)
    elif raw_date:
        try:
            d = date.fromisoformat(raw_date)
        except ValueError:
            return Clarify("What date did this happen?")
    else:
        d = today
    if d > today:
        return Clarify("That date is in the future. What date did this happen?")
    if (today - d).days > MAX_AGE_DAYS:
        return Clarify("That date is more than a year ago. What date did this happen?")
    return d


def _join_desc(items: list[ItemDraft]) -> str:
    s = ", ".join(i.description for i in items)
    return s if len(s) <= 80 else s[:77] + "..."


# --------------------------------------------------------------------------- builders
def build(raw: dict, store: Store, text: str, today: date):
    """Turn validated raw intent into an actionable plan. `text` is the full user text (incl. clarifications)."""
    intent = raw["intent"]
    if intent == "clarify":
        return Clarify(raw["clarification_question"], from_model=True)
    if intent == "other":
        return Other(raw["reply"])
    if intent == "query":
        q = raw["query"]
        cat = store.resolve_category(q["category"]) if q["category"] else None
        return Query(q["type"], cat["id"] if cat else None, q["category"])
    if intent == "purchase_question":
        p = raw["purchase"]
        price = _cents(p["price"], text, "price")
        if isinstance(price, Clarify):
            return Clarify("What is the price?")
        cat = store.resolve_category(p["category"]) or store.resolve_category(guess_category(p["item"]))
        return PurchaseQ(p["item"], price, p["recurring"], cat["id"] if cat else None, p["need"], p["asks_timing"])
    return _build_tx(raw["transaction"], store, text, today)


def _build_tx(tx: dict, store: Store, text: str, today: date):
    ttype = tx["type"]
    raw_items = [i for i in tx["items"] if i["description"] or i["unit_price"] or i["line_total"]]
    stated = _cents(tx["stated_total"], text, "total amount paid")
    if isinstance(stated, Clarify):
        return stated
    if not raw_items:
        if stated is None:
            return Clarify("What was the total amount paid?")
        raw_items = [{"description": None, "quantity": 1, "unit_singular": None, "unit_plural": None,
                      "unit_price": None, "line_total": tx["stated_total"]}]

    items: list[ItemDraft] = []
    for it in raw_items:
        desc = it["description"] or {"income": "Income", "refund": "Refund", "transfer": "Transfer"}.get(ttype)
        if not desc:
            return Clarify("What did you buy?")
        q = it["quantity"] if it["quantity"] is not None else 1
        if not (1 <= q <= MAX_QTY):
            return Clarify(f"How many {desc} did you buy?")
        if not _quantity_in_text(q, text):
            return Clarify(f"How many {desc} did you buy?")
        unit = _cents(it["unit_price"], text, "unit price")
        line = _cents(it["line_total"], text, "total amount paid")
        for v in (unit, line):
            if isinstance(v, Clarify):
                return v
        if unit is not None:
            calc = unit * q
            if line is not None and line != calc:
                return Clarify(f"{desc}: {q} x {fmt(unit)} is {fmt(calc)}, but the total given is {fmt(line)}. "
                               "What was the total amount paid?")
            line = calc
        elif line is not None:
            unit = line // q if line % q == 0 else None
        else:
            if len(raw_items) == 1 and stated is not None:
                line = stated
                unit = line // q if line % q == 0 else None
            else:
                return Clarify("What was the total amount paid?" if len(raw_items) == 1
                               else f"What did the {desc} cost?")
        us, up = it["unit_singular"], it["unit_plural"]
        if us and not up:
            up = us + "s"
        items.append(ItemDraft(desc[:1].upper() + desc[1:], q, line, unit, us, up))

    total = sum(i.line_total_cents for i in items)
    if stated is not None and stated != total:
        return Clarify(f"The items add up to {fmt(total)}, but the total given is {fmt(stated)}. "
                       "What was the total amount paid?")
    if ttype != "expense" and len(items) > 1:
        return Clarify("Please log one income, refund or transfer at a time. What was the amount?")

    d = resolve_date(tx["date"], text, today)
    if isinstance(d, Clarify):
        return d
    draft = TxDraft(type=ttype, description=_join_desc(items), items=items, effective_date=d)

    if tx["account"] and ttype != "transfer":
        acct = store.resolve_account(tx["account"])
        if acct:
            draft.account_id = acct["id"]
        else:
            draft.notes.append(f"No account named '{tx['account']}'; recorded without an account.")

    if ttype == "expense":
        cat = store.resolve_category(tx["category"]) or store.resolve_category(guess_category(draft.description))
        if cat is None:
            cat = store.category_by_kind("uncategorised")
            draft.notes.append("Category: Uncategorised. Set it with /edit {id} category <name>.")
        draft.category_id = cat["id"]
    elif ttype == "income":
        draft.income_kind = tx["income_kind"] or "other"
    elif ttype == "refund":
        r = _link_refund(tx, store, draft, today)
        if r is not None:
            return r
    elif ttype == "transfer":
        src = store.resolve_account(tx["from_account"])
        dst = store.resolve_account(tx["to_account"])
        if not tx["from_account"] or not tx["to_account"]:
            return Clarify("Which account did the money move from, and to?")
        if src is None or dst is None:
            missing = tx["from_account"] if src is None else tx["to_account"]
            return Reject(f"Not saved. No account named '{missing}'. Transfers move money between accounts you "
                          "track: add it with /accounts add <name> <type> <balance>.")
        if src["id"] == dst["id"]:
            return Reject("Not saved. A transfer needs two different accounts.")
        draft.from_account_id, draft.to_account_id = src["id"], dst["id"]
        draft.description = f"{src['name']} → {dst['name']}"
        draft.items[0].description = draft.description
    return TxPlan(draft)


def _link_refund(tx: dict, store: Store, draft: TxDraft, today: date):
    probe = tx["refund_of"] or draft.description
    probe = re.sub(r"\brefund(ed)?\b", "", probe, flags=re.I).strip()
    matches = store.search_expenses(probe, today - timedelta(days=120)) if len(probe) >= 3 else []
    if len(matches) == 1:
        orig = matches[0]
        left = orig["total_cents"] - store.refunded_total(orig["id"])
        if draft.total_cents > left:
            return Clarify(f"That is more than the {fmt(left)} still refundable on #{orig['id']} "
                           f"({orig['description']}). What amount was refunded?")
        draft.related_transaction_id = orig["id"]
        draft.category_id = orig["category_id"]
        cat = store.category(orig["category_id"]) if orig["category_id"] else None
        draft.notes.append(f"Linked to #{orig['id']} ({orig['description']}"
                           + (f", {cat['name']})." if cat else ")."))
        return None
    cat = store.resolve_category(tx["category"]) or store.resolve_category(guess_category(probe))
    if cat is None:
        cat = store.category_by_kind("uncategorised")
    draft.category_id = cat["id"]
    why = "matches several purchases" if matches else "no matching purchase found"
    draft.notes.append(f"Not linked to an original purchase ({why}); counted against {cat['name']}.")
    return None
