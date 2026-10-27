"""Slash-command handlers. Each returns the reply text; ledger writes record idempotency atomically."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from . import report
from .finance import balances, due_commitments, goal_statuses, summarise_month
from .fmt import confirmation, qty_phrase, tx_line
from .money import MAX_CENTS, MoneyError, fmt, parse_cents
from .store import ACCOUNT_KINDS, ItemDraft, Store, StoreError, TxDraft
from .timeutil import parse_date_arg, short_date
from .ui import HELP, HELP_TOPICS


@dataclass
class Ctx:
    store: Store
    today: date
    update_id: int
    chat_id: int
    message_id: int | None
    briefing_hour: int = 7


def _amount(tok: str) -> int:
    try:
        return parse_cents(tok)
    except MoneyError:
        raise StoreError(f"'{tok}' isn't an amount I can use. Write it like 12 or 12.50.") from None


def _cat(ctx: Ctx, name: str, include_inactive: bool = False):
    c = ctx.store.resolve_category(name, include_inactive=include_inactive)
    if c is None:
        names = ", ".join(r["name"] for r in ctx.store.categories())
        raise StoreError(f"I don't have a category called '{name}'. Categories: {names}.")
    return c


# ------------------------------------------------------------------------------ /start /help
def start(ctx: Ctx) -> str:
    st = ctx.store
    limits = st.active_limits()
    lines = [
        "KRONA. Every dollar, accounted for.",
        "",
        "1. Log spending by typing it: lunch 12",
        "2. Set a monthly limit: overall budget 1500",
        "3. Tap Today or Report to see where you stand",
        "",
        "Ask before buying: should i buy shoes for $120?",
        f"Every morning at {ctx.briefing_hour:02d}:00 you get a short briefing here.",
    ]
    if None in limits:
        lines[3] = f"2. Monthly limit: {fmt(limits[None])} (change: overall budget 1500)"
    return "\n".join(lines)


def help_text(args: list[str]) -> str:
    topic = args[0].lower().rstrip("s") + "s" if args else ""
    for key in (args[0].lower() if args else "", topic, topic.rstrip("s")):
        if key in HELP_TOPICS:
            return HELP_TOPICS[key]
    return HELP


# ------------------------------------------------------------------------------ /budget
def budget(ctx: Ctx, args: list[str]) -> str:
    st = ctx.store
    if not args:
        return budget_overview(ctx)
    sub = args[0].lower()
    rest = args[1:]
    if sub == "overall":
        if not rest:
            raise StoreError("Try: overall budget 1500 (or /budget overall off)")
        if rest[0].lower() in ("off", "clear", "none"):
            st.set_limit(None, None, ctx.today)
            return "Overall limit removed."
        cents = _amount(rest[0])
        st.set_limit(None, cents, ctx.today)
        return f"Monthly spending limit set to {fmt(cents)}. Tap Today to see what you can spend each day."
    if sub == "set":
        if len(rest) < 2:
            raise StoreError("Try: food budget 400")
        if " ".join(rest[:-1]).lower() == "overall":
            return budget(ctx, ["overall", rest[-1]])
        cents = _amount(rest[-1])
        c = _cat(ctx, " ".join(rest[:-1]))
        if c["kind"] == "savings":
            raise StoreError("Savings and investments is an allocation, not spending. Use /goals instead.")
        st.set_limit(c["id"], cents, ctx.today)
        return f"{c['name']}: limit set to {fmt(cents)} a month."
    if sub == "clear":
        if " ".join(rest).lower() == "overall":
            return budget(ctx, ["overall", "off"])
        c = _cat(ctx, " ".join(rest))
        st.set_limit(c["id"], None, ctx.today)
        return f"{c['name']} limit removed."
    if sub == "categories":
        return "Categories\n" + "\n".join(
            f"{r['name']}" + ("" if r["active"] else " [disabled]") + (" (allocation)" if r["kind"] == "savings" else "")
            for r in st.categories(include_inactive=True))
    if sub == "category":
        return _category_cmd(ctx, rest)
    if sub in ("commit", "commitment", "commitments"):
        return _commit_cmd(ctx, rest)
    if sub == "payday":
        if not rest:
            cur = st.get_setting("next_income_date")
            return f"Next expected income: {cur}." if cur else "No expected income date set."
        if rest[0].lower() in ("off", "clear", "none"):
            st.set_setting("next_income_date", None)
            return "Expected income date removed."
        d = parse_date_arg(" ".join(rest), ctx.today)
        if d is None or d < ctx.today:
            raise StoreError("Give a date today or later, e.g. /budget payday 2026-11-10")
        st.set_setting("next_income_date", d.isoformat())
        return f"Next expected income: {short_date(d)}. Income does not raise your limits."
    if sub == "cooloff":
        if not rest:
            raise StoreError("Try: /budget cooloff 100 48 (wait 48 hours before buys of $100+)")
        if rest[0].lower() in ("off", "clear", "none"):
            st.set_setting("cooloff_cents", None)
            return "Cooling-off rule removed."
        cents = _amount(rest[0])
        hours = rest[1] if len(rest) > 1 else "48"
        if not hours.isdigit() or not 1 <= int(hours) <= 720:
            raise StoreError("Hours must be between 1 and 720.")
        st.set_setting("cooloff_cents", str(cents))
        st.set_setting("cooloff_hours", hours)
        return f"Cooling-off rule: wait {hours} hours on discretionary purchases of {fmt(cents)} or more."
    # Shorthand: /budget food 400, /budget food off, /budget 1500 (overall)
    if len(args) == 1:
        args = ["overall", args[0]]
    if args[-1].lower() in ("off", "clear", "none", "remove"):
        return budget(ctx, ["clear", *args[:-1]])
    try:
        parse_cents(args[-1])
    except MoneyError:
        raise StoreError("Try: /budget food 400 or /budget overall 1500. /help budget shows more.") from None
    return budget(ctx, ["set", *args])


def budget_overview(ctx: Ctx) -> str:
    s = summarise_month(ctx.store, ctx.today, through=ctx.today)
    days_left = (s.end - ctx.today).days + 1
    lines = [f"BUDGET · {ctx.today.strftime('%B')} ({days_left} day{'s' if days_left != 1 else ''} left)"]
    if s.overall_limit:
        lines.append(f"All spending: {fmt(s.overall_remaining)} left of {fmt(s.overall_limit)} "
                     f"({s.overall_pct}% used)" + (" - limit reached" if s.overall_remaining <= 0 else ""))
    else:
        lines.append(f"All spending: {fmt(s.net_spending)} so far, no monthly limit yet")
    limited = [c for c in s.categories.values() if c.limit]
    for c in limited:
        flag = " - over" if c.net > c.limit else (" - limit reached" if c.net == c.limit else "")
        lines.append(f"{c.name}: {fmt(max(0, c.remaining))} left of {fmt(c.limit)}{flag}")
    dues = due_commitments(ctx.store, ctx.today)
    if dues:
        lines.append("Bills still to pay: " + ", ".join(f"{d.name} {fmt(d.amount)} ({short_date(d.due)})" for d in dues))
    lines.append("")
    if not s.overall_limit and not limited:
        lines.append("Set one: overall budget 1500 · food budget 400")
    else:
        lines.append("Change: food budget 400 · overall budget 1500 · /help budget")
    return "\n".join(lines)


def _category_cmd(ctx: Ctx, rest: list[str]) -> str:
    st = ctx.store
    if not rest:
        raise StoreError("Try: /budget category add Pets (more: /help budget)")
    action, args = rest[0].lower(), " ".join(rest[1:])
    if action == "add":
        c = st.add_category(args)
        return f"Category added: {c['name']}."
    if action in ("rename", "move"):
        m = re.match(r"(.+?)\s+to\s+(.+)$", args, re.I)
        if not m:
            raise StoreError(f"Try: /budget category {action} Travel to Holidays")
        src = _cat(ctx, m.group(1), include_inactive=True)
        if action == "rename":
            st.rename_category(src["id"], m.group(2))
            return f"Renamed {src['name']} to {' '.join(m.group(2).split())}."
        dst = _cat(ctx, m.group(2))
        if dst["id"] == src["id"]:
            raise StoreError("Choose two different categories.")
        n = st.reassign_category(src["id"], dst["id"])
        return f"Moved {n} entr{'y' if n == 1 else 'ies'} from {src['name']} to {dst['name']}."
    if action in ("disable", "enable"):
        c = _cat(ctx, args, include_inactive=True)
        st.set_category_active(c["id"], action == "enable")
        return f"{c['name']} {action}d." + (" Its limit was removed." if action == "disable" else "")
    raise StoreError("Unknown category action. Use add, rename, disable, enable or move.")


def _commit_cmd(ctx: Ctx, rest: list[str]) -> str:
    st = ctx.store
    if not rest or rest[0].lower() == "list":
        items = st.commitments()
        if not items:
            return "No commitments. Add one: /budget commit add Rent 800 day 1"
        due = {d.id: d for d in due_commitments(st, ctx.today)}
        lines = ["Commitments"]
        for c in items:
            when = f"day {c['due_day']} monthly" if c["due_day"] else f"on {c['due_date']}"
            state = "unpaid this month" if c["id"] in due else "paid or not due this month"
            lines.append(f"{c['name']}: {fmt(c['amount_cents'])}, {when}, {state}")
        return "\n".join(lines)
    action, args = rest[0].lower(), " ".join(rest[1:])
    if action == "add":
        m = re.match(r"(.+?)\s+(\S+)\s+(?:day\s+(\d{1,2})|on\s+(\S+))(?:\s+category\s+(.+))?$", args, re.I)
        if not m:
            raise StoreError("Try: /budget commit add Rent 800 day 1, or /budget commit add Course 300 on 2026-11-15")
        name, cents = m.group(1), _amount(m.group(2))
        if st.resolve_commitment(name) and st.resolve_commitment(name)["name"].lower() == name.lower():
            raise StoreError(f"A commitment named {name} already exists.")
        day = int(m.group(3)) if m.group(3) else None
        if day is not None and not 1 <= day <= 31:
            raise StoreError("Day must be 1-31.")
        due_date = parse_date_arg(m.group(4), ctx.today) if m.group(4) else None
        if m.group(4) and due_date is None:
            raise StoreError("Use a date like 2026-11-15.")
        cat = _cat(ctx, m.group(5)) if m.group(5) else st.resolve_category("Bills and subscriptions")
        st.add_commitment(name, cents, cat["id"] if cat else None, day, due_date)
        when = f"every month on day {day}" if day else f"on {short_date(due_date)}"
        return f"Commitment added: {name} {fmt(cents)} {when}. It is reserved until paid."
    c = st.resolve_commitment(args)
    if c is None:
        raise StoreError(f"No commitment matches '{args}'.")
    if action == "paid":
        st.mark_commitment_paid(c["id"], ctx.today.strftime("%Y-%m"))
        return f"{c['name']} marked paid for {ctx.today.strftime('%B')}. Log the payment itself if you have not."
    if action == "remove":
        st.deactivate_commitment(c["id"])
        return f"Commitment removed: {c['name']}."
    raise StoreError("Unknown commit action. Use add, list, paid or remove.")


# ------------------------------------------------------------------------------ /accounts
def accounts(ctx: Ctx, args: list[str]) -> str:
    st = ctx.store
    if not args:
        b = balances(st)
        if not b.configured:
            return ("No accounts yet. They're optional: spending logs fine without them.\n"
                    "Add one with its current balance: /accounts add DBS 2500")
        lines = ["ACCOUNTS (balances you entered, not live bank data)"]
        for name, kind, bal, as_of in b.accounts:
            lines.append(f"{name}: {fmt(bal)} ({kind}, last set {short_date(date.fromisoformat(as_of))})")
        lines.append(f"Available to spend from: {fmt(b.liquid)}" + (f", of which {fmt(b.protected)} is protected"
                                                                     if b.protected else ""))
        lines.append("Correct a balance: /accounts reconcile DBS 2410.50")
        return "\n".join(lines)
    action, rest = args[0].lower(), args[1:]
    if action == "add":
        # /accounts add <name> [type] <balance>, or /accounts add <name> <balance> <type>
        # A type is a lowercase type word ("main"); a capitalised one ("My Savings") is part of the name.
        def is_type(tok: str) -> bool:
            return tok in ACCOUNT_KINDS

        if len(rest) >= 3 and is_type(rest[-1]):
            rest = [*rest[:-2], rest[-1], rest[-2]]
        if len(rest) >= 3 and is_type(rest[-2]):
            name, kind = " ".join(rest[:-2]), rest[-2].lower()
        elif len(rest) >= 2:
            name, kind = " ".join(rest[:-1]), _guess_kind(" ".join(rest[:-1]))
        else:
            raise StoreError("Add an account with its current balance: /accounts add DBS 2500")
        opening = _signed_amount(rest[-1])
        a = st.add_account(name, kind, opening, ctx.today)
        return (f"Added {a['name']} ({a['kind']}) with {fmt(opening)}.\n"
                f"Use it like: transferred $100 from {a['name']} to Savings")
    if action == "reconcile":
        if len(rest) < 2:
            raise StoreError("Try: /accounts reconcile DBS 2410.50")
        a = st.resolve_account(" ".join(rest[:-1]))
        if a is None:
            raise StoreError(f"No account named '{' '.join(rest[:-1])}'.")
        before, _ = st.account_balance(a["id"])
        bal = _signed_amount(rest[-1])
        st.reconcile(a["id"], bal, ctx.today)
        diff = bal - before
        return f"{a['name']} reconciled to {fmt(bal)}" + (f" (difference {fmt(diff)} vs KRONA's figure)." if diff else ".")
    if action == "remove":
        a = st.resolve_account(" ".join(rest))
        if a is None:
            raise StoreError(f"No account named '{' '.join(rest)}'.")
        st.deactivate_account(a["id"])
        return f"Account removed from tracking: {a['name']}. Its history is kept."
    raise StoreError("Try: /accounts add DBS 2500 · /accounts reconcile DBS 2410.50 · /help accounts")


def _guess_kind(name: str) -> str:
    n = name.lower()
    for word, kind in (("saving", "savings"), ("invest", "investment"), ("brokerage", "investment"),
                       ("cash", "cash"), ("wallet", "cash"), ("spend", "spending")):
        if word in n:
            return kind
    return "main"


def _signed_amount(tok: str) -> int:
    neg = tok.startswith("-")
    t = tok[1:] if neg else tok
    try:
        c = parse_cents(t, allow_zero=True)
    except MoneyError:
        raise StoreError(f"'{tok}' isn't an amount I can use. Write it like 12 or 12.50.") from None
    return -c if neg else c


# ------------------------------------------------------------------------------ /goals
def goals(ctx: Ctx, args: list[str]) -> str:
    st = ctx.store
    if not args:
        gs = goal_statuses(st, ctx.today)
        prot = int(st.get_setting("protected_cents", "0") or 0)
        if not gs and not prot:
            return ("No savings goals yet. They're optional.\n"
                    "Add one: /goals add Emergency 5000\nMore options: /help goals")
        lines = ["GOALS"]
        for g in gs:
            line = f"{g.name}: target {fmt(g.target)}"
            if g.progress is not None:
                line += f", linked balance {fmt(g.progress)} ({min(100, g.progress * 100 // g.target)}%)"
            else:
                line += ", progress not tracked (no linked account)"
            if g.monthly:
                line += f"; {fmt(g.contributed_this_month or 0)} of {fmt(g.monthly)} allocated this month"
            lines.append(line)
        if prot:
            lines.append(f"Protected funds: {fmt(prot)} (never counted as spendable)")
        return "\n".join(lines)
    action, rest = args[0].lower(), " ".join(args[1:])
    if action == "add":
        m = re.match(r"(.+?)\s+(\S+)(?:\s+monthly\s+(\S+))?(?:\s+account\s+(.+?))?(?:\s+by\s+(\S+))?$", rest, re.I)
        if not m:
            raise StoreError("Try: /goals add Emergency 5000 (more options: /help goals)")
        name = m.group(1)
        if any(g["name"].lower() == name.lower() for g in st.goals()):
            raise StoreError(f"A goal named {name} already exists.")
        target = _amount(m.group(2))
        monthly = _amount(m.group(3)) if m.group(3) else None
        acct = None
        if m.group(4):
            acct = st.resolve_account(m.group(4))
            if acct is None:
                raise StoreError(f"No account named '{m.group(4)}'.")
        by = parse_date_arg(m.group(5), ctx.today) if m.group(5) else None
        st.add_goal(name, target, monthly, acct["id"] if acct else None, by)
        return (f"Goal added: {name}, target {fmt(target)}" + (f", {fmt(monthly)}/month" if monthly else "")
                + (f", tracked in {acct['name']}" if acct else "") + ".")
    if action == "remove":
        if not st.deactivate_goal(rest):
            raise StoreError(f"No goal named '{rest}'.")
        return f"Goal removed: {rest}."
    if action == "protect":
        if rest.lower() in ("off", "clear", "none", "0"):
            st.set_setting("protected_cents", None)
            return "Protected funds removed."
        cents = _amount(rest)
        st.set_setting("protected_cents", str(cents))
        return f"Protected funds: {fmt(cents)}. KRONA will never count this as spendable."
    raise StoreError("Try: /goals add Emergency 5000 · /goals remove Emergency · /help goals")


# ------------------------------------------------------------------------------ /history
def history(ctx: Ctx, args: list[str]) -> str:
    show_all = bool(args) and args[0].lower() == "all"
    n = 10
    if args and args[-1].isdigit():
        n = max(1, min(50, int(args[-1])))
    rows = ctx.store.recent(n, include_inactive=show_all)
    if not rows:
        return "Nothing logged yet. Type what you spent, like: lunch 12"
    return ("RECENT ENTRIES\n" + "\n".join(tx_line(ctx.store, r, show_status=show_all) for r in rows)
            + "\n\nFix one: /edit 3 15.20 (the # number) · Undo removes the latest")


# ------------------------------------------------------------------------------ /undo
def undo(ctx: Ctx, args: list[str]) -> str:
    st = ctx.store
    if args and args[0].lstrip("#").isdigit():
        t = st.tx(int(args[0].lstrip("#")))
        if t is None or t["status"] != "active":
            raise StoreError(f"There's no entry #{args[0].lstrip('#')} to undo. Check History for the number.")
        return do_reverse(ctx, t["id"])
    if args:
        rows = st.search_active(" ".join(args), limit=5)
        if not rows:
            raise StoreError(f"Nothing matches '{' '.join(args)}'. Check History.")
        if len(rows) > 1:
            st.set_pending("choose_undo", {"ids": [r["id"] for r in rows]})
            return "Which one should I undo? Tap its number.\n" + "\n".join(
                f"{i}. {_brief(st, r)}" for i, r in enumerate(rows, start=1))
        target = rows[0]
    else:
        recent = st.recent(1)
        if not recent:
            raise StoreError("Nothing to undo.")
        target = recent[0]
    st.set_pending("confirm_undo", {"id": target["id"]})
    return f"Undo this entry?\n{_brief(st, target)}"


def _brief(st: Store, t) -> str:
    sign = {"income": "+", "refund": "+"}.get(t["type"], "")
    return f"{t['description']} {sign}{fmt(t['total_cents'])} · {short_date(date.fromisoformat(t['effective_date']))}"


def do_reverse(ctx: Ctx, tx_id: int) -> str:
    st = ctx.store
    t = st.tx(tx_id)
    reply = f"Undone: {_brief(st, t)}. It no longer counts."
    if t["type"] == "expense":
        linked = st.refunded_total(tx_id)
        if linked:
            reply += f"\nNote: {fmt(linked)} of refunds linked to this purchase are still recorded."
    st.reverse(tx_id, ctx.update_id, ctx.chat_id, reply)
    return reply


# ------------------------------------------------------------------------------ /edit
FIELDS = {"amount": "amount", "total": "amount", "price": "price", "unitprice": "price", "unit": "price",
          "quantity": "quantity", "qty": "quantity", "description": "description", "desc": "description",
          "name": "description", "category": "category", "cat": "category", "date": "date"}


def _normalise_edit(ctx: Ctx, args: list[str]) -> list[str] | None:
    """Accept the forms people try and turn them into [id, field, value...].

    /edit 15.20               last entry's amount
    /edit 3 15.20             entry #3's amount
    /edit lunch 15.20         latest entry matching "lunch"
    /edit lunch category food latest "lunch" entry, any field
    /edit 3 amount 15.20      the full form
    """
    st = ctx.store
    if not args:
        return None
    is_id = args[0].lstrip("#").isdigit() and len(args) >= 2
    if is_id:
        target, rest = args[0].lstrip("#"), args[1:]
    else:
        idx = next((i for i, a in enumerate(args) if a.lower() in FIELDS or a.lower() == "item"), None)
        if idx is not None:
            words, rest = args[:idx], args[idx:]
        else:
            words, rest = args[:-1], args[-1:]
        if words:
            rows = st.search_active(" ".join(words), limit=1)
            if not rows:
                raise StoreError(f"Nothing matches '{' '.join(words)}'. Check History for the entry's # number.")
        else:
            rows = st.recent(1)
            if not rows:
                raise StoreError("Nothing to edit yet.")
        target = str(rows[0]["id"])
    if len(rest) == 1:
        rest = ["amount", rest[0]]
    return [target, *rest]


def edit(ctx: Ctx, args: list[str]) -> str:
    st = ctx.store
    norm = _normalise_edit(ctx, args)
    if norm is None or len(norm) < 3:
        recent = st.recent(5)
        lines = ["Fix an entry:",
                 "/edit 15.20: change the last entry's amount",
                 "/edit lunch 15.20: change the last 'lunch' entry",
                 "/edit 3 category Transport: change entry #3 (also: date, description, quantity, price)"]
        if recent:
            lines += ["", "Recent entries"] + [tx_line(st, r) for r in recent]
        return "\n".join(lines)
    args = norm
    tx_id = int(args[0].lstrip("#"))
    t = st.tx(tx_id)
    if t is None or t["status"] != "active":
        raise StoreError(f"There's no entry #{tx_id} to edit. Check History for the number.")
    rest = args[1:]
    line_no = None
    if rest[0].lower() == "item":
        if len(rest) < 4 or not rest[1].isdigit():
            raise StoreError("Try: /edit 3 item 2 quantity 3")
        line_no, rest = int(rest[1]), rest[2:]
    field = FIELDS.get(rest[0].lower())
    if field is None:
        raise StoreError("I can change: amount, price, quantity, description, category or date. "
                         "Example: /edit 3 category Transport")
    value = " ".join(rest[1:]).strip()
    items = st.items(tx_id)
    new_items = [ItemDraft(i["description"], i["quantity"], i["line_total_cents"], i["unit_price_cents"],
                           i["unit_singular"], i["unit_plural"]) for i in items]
    d = TxDraft(type=t["type"], description=t["description"], items=new_items,
                effective_date=date.fromisoformat(t["effective_date"]), category_id=t["category_id"],
                account_id=t["account_id"], from_account_id=t["from_account_id"], to_account_id=t["to_account_id"],
                related_transaction_id=t["related_transaction_id"], income_kind=t["income_kind"])
    if field in ("amount", "price", "quantity") or (field == "description" and line_no):
        if line_no is None:
            if len(new_items) > 1:
                raise StoreError(f"#{tx_id} has {len(new_items)} lines. Use /edit {tx_id} item <n> {rest[0]} <value>.")
            line_no = 1
        if not 1 <= line_no <= len(new_items):
            raise StoreError(f"#{tx_id} has no item {line_no}.")
        it = new_items[line_no - 1]
        if field == "amount":
            it.line_total_cents = _amount(value)
            it.unit_price_cents = it.line_total_cents // it.quantity if it.line_total_cents % it.quantity == 0 else None
        elif field == "price":
            it.unit_price_cents = _amount(value)
            it.line_total_cents = it.unit_price_cents * it.quantity
        elif field == "quantity":
            if not value.isdigit() or not 1 <= int(value) <= 1000:
                raise StoreError("Quantity must be a whole number from 1 to 1000.")
            if t["type"] != "expense":
                raise StoreError("Only purchases have quantities.")
            q = int(value)
            if it.unit_price_cents is None:
                raise StoreError("The unit price of this line is unknown. Set it with: "
                                 f"/edit {tx_id} price <amount> after the quantity, or edit the amount.")
            it.quantity = q
            it.line_total_cents = it.unit_price_cents * q
        else:
            it.description = value[:80]
        if len(new_items) == 1 and t["type"] != "transfer" and field == "description":
            d.description = it.description
        if it.line_total_cents > MAX_CENTS:
            raise StoreError("Amount is implausibly large.")
    elif field == "description":
        if not value:
            raise StoreError("Give a description.")
        d.description = value[:80]
        if len(new_items) == 1:
            new_items[0].description = d.description
    elif field == "category":
        if t["type"] not in ("expense", "refund"):
            raise StoreError("Only purchases and refunds have categories.")
        c = _cat(ctx, value)
        d.category_id = c["id"]
    elif field == "date":
        nd = parse_date_arg(value, ctx.today)
        if nd is None:
            raise StoreError("Use a date like 2026-10-24, '24 Oct' or 'yesterday'.")
        if nd > ctx.today:
            raise StoreError("The date cannot be in the future.")
        d.effective_date = nd
    if t["type"] == "refund" and t["related_transaction_id"]:
        left = st.tx(t["related_transaction_id"])["total_cents"] - st.refunded_total(t["related_transaction_id"],
                                                                                      exclude_id=tx_id)
        if d.total_cents > left:
            raise StoreError(f"That exceeds the {fmt(left)} still refundable on #{t['related_transaction_id']}.")
    changes = {"field": field, "value": value, "item": line_no}

    def make_reply(new_id: int) -> str:
        body = confirmation(d, new_id, ctx.today).split("\n", 1)[1]
        return f"Fixed.\n{body}"

    new_id = st.correct(tx_id, d, changes, ctx.update_id, ctx.chat_id, make_reply)
    return make_reply(new_id)


def report_cmd(ctx: Ctx, args: list[str]) -> str:
    month = None
    if args:
        a = args[0].lower()
        if a in ("last", "previous", "prev"):
            from .timeutil import previous_month
            month = previous_month(ctx.today)
        elif re.fullmatch(r"\d{4}-\d{2}", a):
            month = date(int(a[:4]), int(a[5:]), 1)
            if month > ctx.today:
                raise StoreError("That month has not started.")
        else:
            raise StoreError("Try: /report, /report last or /report 2026-09")
    return report.build_report(ctx.store, ctx.today, month)


__all__ = ["Ctx", "HELP", "start", "budget", "accounts", "goals", "history", "undo", "do_reverse", "edit",
           "report_cmd", "qty_phrase"]
