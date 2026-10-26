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
    except MoneyError as exc:
        raise StoreError(f"'{tok}' is not a valid amount ({exc}).") from None


def _cat(ctx: Ctx, name: str, include_inactive: bool = False):
    c = ctx.store.resolve_category(name, include_inactive=include_inactive)
    if c is None:
        names = ", ".join(r["name"] for r in ctx.store.categories())
        raise StoreError(f"No category matches '{name}'. Categories: {names}.")
    return c


# ------------------------------------------------------------------------------ /start /help
def start(ctx: Ctx) -> str:
    st = ctx.store
    limits = st.active_limits()
    cat_limits = sum(1 for k in limits if k is not None)
    lines = [
        "KRONA",
        "Every dollar, accounted for.",
        "",
        "Log activity in plain language:",
        "- Spent $12 on lunch",
        "- Bought 2 bottles of protein milk at $3.80 each",
        "- Received $900 internship pay",
        "- Received a $20 refund for shoes",
        "- Transferred $100 from Main to Savings",
        "",
        "Ask before buying: should i buy these shoes for $120?",
        "Review the month: /report",
        "",
        "Setup (do it in any order; logging works now):",
        f"[{'set' if None in limits else 'not set'}] Overall monthly limit: /budget overall 1200",
        f"[{cat_limits} set] Category limits: /budget set Food 400",
        f"[{len(st.accounts())} added] Accounts, optional: /accounts add Main main 2500",
        f"[{len(st.goals())} added] Savings goals, optional: /goals add Emergency 5000",
        f"[{len(st.commitments())} added] Upcoming bills, optional: /budget commit add Rent 800 day 1",
        "",
        f"Daily briefing: {ctx.briefing_hour:02d}:00 Singapore time. /help lists every command.",
    ]
    if None not in limits:
        lines.insert(14, "Until a limit is set, KRONA records spending but cannot warn or give a daily allowance.")
    return "\n".join(lines)


HELP = """KRONA commands
/report - full review of this month (/report last for last month)
/budget - limits and usage
  /budget overall 1200 | /budget overall off
  /budget set Food 400 | /budget clear Food
  /budget categories | category add Pets | category rename Pets to Pet care
  /budget category disable Travel | category enable Travel | category move Travel to Shopping
  /budget commit add Rent 800 day 1 | commit add Course fee 300 on 2026-11-15
  /budget commit list | commit paid Rent | commit remove Rent
  /budget payday 2026-11-10 | payday off
  /budget cooloff 100 48 | cooloff off  (wait 48h on discretionary buys of $100+)
/history - recent entries (/history 20, /history all)
/undo - reverse the last entry (/undo 12, /undo lunch)
/edit 12 amount 15.20 - also: price, quantity, description, category, date
  multi-line entries: /edit 12 item 2 quantity 3
/accounts - balances | /accounts add Main main 2500 | reconcile Main 2410.50 | remove Main
  types: main, spending, savings, investment, cash, other
/goals - savings goals | /goals add Emergency 5000 monthly 300 account Savings by 2027-06-30
  /goals remove Emergency | /goals protect 2000 | /goals protect off
/help - this list

Plain-language examples
Spent $12 on lunch
I spent $18.50 on dinner yesterday
Received $60 bonus from my shift
should i buy a $45 game?
How much can I safely spend today?
Compare this month to last month"""


# ------------------------------------------------------------------------------ /budget
def budget(ctx: Ctx, args: list[str]) -> str:
    st = ctx.store
    if not args:
        return budget_overview(ctx)
    sub = args[0].lower()
    rest = args[1:]
    if sub == "overall":
        if not rest:
            raise StoreError("Usage: /budget overall 1200 (or /budget overall off)")
        if rest[0].lower() in ("off", "clear", "none"):
            st.set_limit(None, None, ctx.today)
            return "Overall limit removed."
        cents = _amount(rest[0])
        st.set_limit(None, cents, ctx.today)
        return f"Overall monthly spending limit set to {fmt(cents)}."
    if sub == "set":
        if len(rest) < 2:
            raise StoreError("Usage: /budget set <category> <amount>")
        cents = _amount(rest[-1])
        c = _cat(ctx, " ".join(rest[:-1]))
        if c["kind"] == "savings":
            raise StoreError("Savings and investments is an allocation, not spending. Use /goals instead.")
        st.set_limit(c["id"], cents, ctx.today)
        return f"{c['name']} limit set to {fmt(cents)} per month."
    if sub == "clear":
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
            raise StoreError("Usage: /budget cooloff <amount> [hours] (or off)")
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
    raise StoreError("Unknown /budget option. See /help.")


def budget_overview(ctx: Ctx) -> str:
    s = summarise_month(ctx.store, ctx.today, through=ctx.today)
    lines = [f"BUDGET | {ctx.today.strftime('%B %Y')}"]
    if s.overall_limit:
        lines.append(f"Overall: {fmt(s.net_spending)} of {fmt(s.overall_limit)} ({s.overall_pct}%), "
                     f"{fmt(s.overall_remaining)} left")
    else:
        lines.append(f"Overall: {fmt(s.net_spending)} spent, no limit set (/budget overall <amount>)")
    any_limit = False
    for c in s.categories.values():
        if c.limit:
            any_limit = True
            lines.append(f"{c.name}: {fmt(c.net)} of {fmt(c.limit)} ({c.pct}%), {fmt(c.remaining)} left")
    if not any_limit:
        lines.append("No category limits set (/budget set <category> <amount>).")
    dues = due_commitments(ctx.store, ctx.today)
    if dues:
        lines.append("Unpaid commitments: " + ", ".join(f"{d.name} {fmt(d.amount)} ({short_date(d.due)})" for d in dues))
    return "\n".join(lines)


def _category_cmd(ctx: Ctx, rest: list[str]) -> str:
    st = ctx.store
    if not rest:
        raise StoreError("Usage: /budget category add|rename|disable|enable|move ...")
    action, args = rest[0].lower(), " ".join(rest[1:])
    if action == "add":
        c = st.add_category(args)
        return f"Category added: {c['name']}."
    if action in ("rename", "move"):
        m = re.match(r"(.+?)\s+to\s+(.+)$", args, re.I)
        if not m:
            raise StoreError(f"Usage: /budget category {action} <old> to <new>")
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
            raise StoreError("Usage: /budget commit add <name> <amount> day <1-31> | on <YYYY-MM-DD> "
                             "[category <name>]")
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
            return ("No accounts recorded. Transactions still log without one.\n"
                    "Add: /accounts add Main main 2500 (types: " + ", ".join(ACCOUNT_KINDS) + ")")
        lines = ["ACCOUNTS (user-maintained figures, not live bank balances)"]
        for name, kind, bal, as_of in b.accounts:
            lines.append(f"{name} ({kind}): {fmt(bal)}, reconciled {short_date(date.fromisoformat(as_of))}")
        lines.append(f"Spendable accounts total: {fmt(b.liquid)}" + (f"; protected {fmt(b.protected)}" if b.protected else ""))
        lines.append("Only transactions logged against an account change its balance.")
        return "\n".join(lines)
    action, rest = args[0].lower(), args[1:]
    if action == "add":
        if len(rest) < 3 or rest[-2].lower() not in ACCOUNT_KINDS:
            raise StoreError("Usage: /accounts add <name> <type> <opening balance>. Types: " + ", ".join(ACCOUNT_KINDS))
        opening = _signed_amount(rest[-1])
        a = st.add_account(" ".join(rest[:-2]), rest[-2].lower(), opening, ctx.today)
        return f"Account added: {a['name']} ({a['kind']}), opening balance {fmt(opening)}."
    if action == "reconcile":
        if len(rest) < 2:
            raise StoreError("Usage: /accounts reconcile <name> <balance>")
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
    raise StoreError("Unknown /accounts option. Use add, reconcile or remove.")


def _signed_amount(tok: str) -> int:
    neg = tok.startswith("-")
    t = tok[1:] if neg else tok
    try:
        c = parse_cents(t, allow_zero=True)
    except MoneyError as exc:
        raise StoreError(f"'{tok}' is not a valid amount ({exc}).") from None
    return -c if neg else c


# ------------------------------------------------------------------------------ /goals
def goals(ctx: Ctx, args: list[str]) -> str:
    st = ctx.store
    if not args:
        gs = goal_statuses(st, ctx.today)
        prot = int(st.get_setting("protected_cents", "0") or 0)
        if not gs and not prot:
            return ("No savings goals or protected funds set.\n"
                    "Add: /goals add Emergency 5000 monthly 300 account Savings\nProtect: /goals protect 2000")
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
            raise StoreError("Usage: /goals add <name> <target> [monthly <amount>] [account <name>] [by YYYY-MM-DD]")
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
    raise StoreError("Unknown /goals option. Use add, remove or protect.")


# ------------------------------------------------------------------------------ /history
def history(ctx: Ctx, args: list[str]) -> str:
    show_all = bool(args) and args[0].lower() == "all"
    n = 10
    if args and args[-1].isdigit():
        n = max(1, min(50, int(args[-1])))
    rows = ctx.store.recent(n, include_inactive=show_all)
    if not rows:
        return "No transactions recorded yet."
    return "Recent entries\n" + "\n".join(tx_line(ctx.store, r, show_status=show_all) for r in rows)


# ------------------------------------------------------------------------------ /undo
def undo(ctx: Ctx, args: list[str]) -> str:
    st = ctx.store
    if args and args[0].lstrip("#").isdigit():
        t = st.tx(int(args[0].lstrip("#")))
        if t is None or t["status"] != "active":
            raise StoreError(f"No active entry #{args[0].lstrip('#')}.")
        return do_reverse(ctx, t["id"])
    if args:
        rows = st.search_active(" ".join(args), limit=5)
        if not rows:
            raise StoreError(f"No active entry matches '{' '.join(args)}'.")
        if len(rows) > 1:
            st.set_pending("choose_undo", {"ids": [r["id"] for r in rows]})
            return "Which one? Reply with the number.\n" + "\n".join(
                f"{i}. {tx_line(st, r)}" for i, r in enumerate(rows, start=1))
        target = rows[0]
    else:
        recent = st.recent(1)
        if not recent:
            raise StoreError("Nothing to undo.")
        target = recent[0]
    st.set_pending("confirm_undo", {"id": target["id"]})
    return f"Reverse {tx_line(st, target)}? Reply yes or no."


def do_reverse(ctx: Ctx, tx_id: int) -> str:
    st = ctx.store
    t = st.tx(tx_id)
    reply = f"Reversed. {tx_line(st, t)}"
    if t["type"] == "expense":
        linked = st.refunded_total(tx_id)
        if linked:
            reply += f"\nNote: {fmt(linked)} of refunds linked to this purchase remain recorded."
    st.reverse(tx_id, ctx.update_id, ctx.chat_id, reply)
    return reply


# ------------------------------------------------------------------------------ /edit
FIELDS = {"amount": "amount", "total": "amount", "price": "price", "unitprice": "price", "unit": "price",
          "quantity": "quantity", "qty": "quantity", "description": "description", "desc": "description",
          "name": "description", "category": "category", "cat": "category", "date": "date"}


def edit(ctx: Ctx, args: list[str]) -> str:
    st = ctx.store
    if len(args) < 3 or not args[0].lstrip("#").isdigit():
        recent = st.recent(5)
        lines = ["Usage: /edit <id> <field> <value>",
                 "Fields: amount, price, quantity, description, category, date",
                 "Example: /edit 12 amount 15.20"]
        if recent:
            lines += ["", "Recent entries"] + [tx_line(st, r) for r in recent]
        return "\n".join(lines)
    tx_id = int(args[0].lstrip("#"))
    t = st.tx(tx_id)
    if t is None or t["status"] != "active":
        raise StoreError(f"No active entry #{tx_id}. Only active entries can be edited.")
    rest = args[1:]
    line_no = None
    if rest[0].lower() == "item":
        if len(rest) < 4 or not rest[1].isdigit():
            raise StoreError("Usage: /edit <id> item <n> <field> <value>")
        line_no, rest = int(rest[1]), rest[2:]
    field = FIELDS.get(rest[0].lower())
    if field is None:
        raise StoreError("Field must be one of: amount, price, quantity, description, category, date.")
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
        return f"Corrected. #{new_id} replaces #{tx_id}.\n{body}"

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
            raise StoreError("Usage: /report, /report last or /report 2026-09")
    return report.build_report(ctx.store, ctx.today, month)


__all__ = ["Ctx", "HELP", "start", "budget", "accounts", "goals", "history", "undo", "do_reverse", "edit",
           "report_cmd", "qty_phrase"]
