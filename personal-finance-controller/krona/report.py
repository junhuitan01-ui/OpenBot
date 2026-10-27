"""/report and finance follow-up answers. Every figure comes from krona.finance."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from .finance import (Allowance, CategoryUsage, MonthSummary, balances, compute_allowance, due_commitments,
                      elapsed_pct, goal_statuses, is_consumption, projected, summarise_month)
from .money import fmt, numbers_in_text
from .store import Store
from .timeutil import days_in_month, month_bounds, previous_month, short_date


def _status(cu: CategoryUsage, today: date, current: bool) -> str:
    if cu.limit is None:
        return "no limit"
    p = cu.pct or 0
    if p >= 100:
        return f"over by {fmt(cu.net - cu.limit)}" if cu.net > cu.limit else "limit reached"
    if current and projected(cu.net, today) > cu.limit and p >= 50:
        return "on track to go over"
    if p >= 90:
        return "almost used up"
    if p >= 75:
        return "getting close"
    return "on track"


def _needs_attention(cu: CategoryUsage, today: date, current: bool) -> bool:
    return _status(cu, today, current) not in ("on track", "no limit")


def _norm_desc(d: str) -> str:
    return re.sub(r"[^a-z ]", "", d.lower()).strip()


def patterns(s: MonthSummary, store: Store, today: date, current: bool) -> list[str]:
    out: list[str] = []
    if not s.expenses:
        return out
    cons = sorted((c for c in s.categories.values() if is_consumption(c.kind) and c.net > 0),
                  key=lambda c: c.net, reverse=True)
    if cons:
        top = cons[:3]
        out.append("Largest categories: " + ", ".join(f"{c.name} {fmt(c.net)}" for c in top) + ".")
    amounts = sorted(t["total_cents"] for t in s.expenses)
    if len(amounts) >= 5:
        median = amounts[len(amounts) // 2]
        big = [t for t in s.expenses if t["total_cents"] >= max(3 * median, 5000)]
        for t in sorted(big, key=lambda t: t["total_cents"], reverse=True)[:2]:
            out.append(f"Large purchase: {t['description']} {fmt(t['total_cents'])} on "
                       f"{short_date(date.fromisoformat(t['effective_date']))}.")
    groups: dict[str, list] = defaultdict(list)
    for t in s.expenses:
        if t["total_cents"] <= 1500:
            groups[_norm_desc(t["description"])].append(t)
    repeated = sorted(((k, v) for k, v in groups.items() if len(v) >= 3 and k),
                      key=lambda kv: sum(t["total_cents"] for t in kv[1]), reverse=True)
    for _, rows in repeated[:2]:
        out.append(f"Repeated small spend: {rows[0]['description']} x{len(rows)} = "
                   f"{fmt(sum(t['total_cents'] for t in rows))}.")
    if current:
        for c in cons:
            if c.limit and c.net <= c.limit < projected(c.net, today):
                out.append(f"{c.name} is on pace for {fmt(projected(c.net, today))} against a "
                           f"{fmt(c.limit)} limit.")
    prev = summarise_month(store, previous_month(s.start),
                           through=previous_month(s.start).replace(
                               day=min(s.through.day, days_in_month(previous_month(s.start)))))
    if prev.tx_count >= 3 and s.tx_count >= 3:
        diff = s.net_spending - prev.net_spending
        direction = "more" if diff > 0 else "less"
        if diff:
            out.append(f"Versus the same point last month: {fmt(abs(diff))} {direction} spent "
                       f"({fmt(s.net_spending)} vs {fmt(prev.net_spending)}).")
    else:
        out.append("Not enough history yet to compare with last month.")
    return out


def allowance_lines(a: Allowance) -> list[str]:
    """Shared 'free to spend' explanation used by Today, the briefing and the report."""
    if a.status == "unavailable":
        return ["Not worked out yet: set a monthly limit first (e.g. overall budget 1500)."]
    if a.status == "provisional":
        lines = ["No overall monthly limit yet, so only per-category figures:"]
        for name, per_day in a.category_daily[:4]:
            lines.append(f"{name}: up to {fmt(per_day)} a day")
        for name in a.exhausted_categories:
            lines.append(f"{name}: used up, spend nothing there")
        return lines
    if a.status == "exhausted":
        why = "your balances, after bills and protected money" if a.binding == "cash" else "this month's limit"
        return [f"{fmt(0)}. Nothing is left in {why}.", "Essentials only; don't touch money set aside for bills."]
    head = f"{fmt(a.amount)} today"
    if a.spent_today:
        head += f" ({fmt(a.remaining_today)} still free after today's spending)"
    lines = [head, f"That's {fmt(max(0, a.overall_left))} left this month spread over {a.days_left} days"
             + (f", after setting aside {fmt(a.committed)} for bills" if a.committed else "") + "."]
    if a.binding == "cash":
        lines.append(f"Lowered to fit your balances: {fmt(max(0, a.cash_available or 0))} available.")
    lines.extend(a.notes)
    for name in a.exhausted_categories:
        lines.append(f"{name}: used up, spend nothing there")
    for name, per_day in a.tight_categories[:3]:
        lines.append(f"{name}: keep it under {fmt(per_day)} today")
    return lines


def today_summary(store: Store, today: date) -> str:
    rows = [r for r in store.active_between(today, today)]
    spent = [r for r in rows if r["type"] == "expense" and is_consumption(r["category_kind"])]
    total = sum(r["total_cents"] for r in spent) - sum(
        r["total_cents"] for r in rows if r["type"] == "refund" and is_consumption(r["category_kind"]))
    lines = [f"TODAY · {short_date(today)}"]
    if spent:
        lines.append(f"Spent {fmt(total)}: " + ", ".join(f"{r['description']} {fmt(r['total_cents'])}"
                                                       for r in spent[:6]) + ("..." if len(spent) > 6 else ""))
    else:
        lines.append("Nothing logged today yet.")
    lines += ["", "FREE TO SPEND", *allowance_lines(compute_allowance(store, today))]
    return "\n".join(lines)


def build_report(store: Store, today: date, month: date | None = None) -> str:
    m = month or today
    current = month_bounds(m) == month_bounds(today)
    s = summarise_month(store, m, through=today if current else None)
    title = m.strftime("%B %Y").upper()
    out: list[str] = [f"KRONA REPORT · {title}" + (f" · to {short_date(today)}" if current else "")]

    # 1. Overview
    spent = f"Spent: {fmt(s.net_spending)}" + (f" (after {fmt(s.refunds)} refunds)" if s.refunds else "")
    net = s.net_cash_flow
    ov = ["OVERVIEW", spent, f"Income: {fmt(s.income)}",
          f"Net: {'+' if net > 0 else ''}{fmt(net)} (income minus everything paid out)"]
    if s.overall_limit:
        ov.append(f"Monthly limit: {fmt(s.overall_limit)}, {fmt(s.overall_remaining)} left ({s.overall_pct}% used)")
    else:
        ov.append("Monthly limit: not set (type: overall budget 1500)")
    if s.savings_outflows:
        ov.append(f"Put into savings/investments: {fmt(s.savings_outflows)} (not counted as spending)")
    if s.transfers_total:
        ov.append(f"Moved between your accounts: {fmt(s.transfers_total)} (not counted as spending or income)")
    if s.reversed_count:
        ov.append(f"Undone entries: {s.reversed_count} (not counted)")
    if s.unlinked_refunds:
        ov.append(f"Refunds not matched to a purchase: {fmt(s.unlinked_refunds)}")
    bal = balances(store)
    if bal.configured:
        ov.append("Balances (as you entered them, not live bank data): "
                  + ", ".join(f"{name} {fmt(b)}" for name, _, b, _ in bal.accounts))
    else:
        ov.append("Balances: none added (optional: /accounts add DBS 2500)")
    if s.tx_count == 0:
        ov.append("Nothing logged for this month.")
    out.append("\n".join(ov))

    # 2. Attention
    limited = [c for c in s.categories.values() if c.limit and is_consumption(c.kind)]
    attention = [c for c in limited if _needs_attention(c, today, current)]
    att = ["NEEDS ATTENTION"]
    if not limited:
        att.append("No category limits yet (type: food budget 400).")
    elif not attention:
        att.append("Nothing. Every category with a limit is on track.")
    for c in sorted(attention, key=lambda c: c.pct or 0, reverse=True):
        att.append(f"{c.name}: {fmt(c.net)} of {fmt(c.limit)} used, {_status(c, today, current)}")
    out.append("\n".join(att))

    # 3. Remaining budget
    rem = ["BY CATEGORY"]
    for c in sorted(s.categories.values(), key=lambda c: c.id):
        if not is_consumption(c.kind) or (c.limit is None and c.net == 0):
            continue
        if c.limit:
            rem.append(f"{c.name}: {fmt(c.net)} spent, {fmt(max(0, c.remaining))} left of {fmt(c.limit)} "
                       f"({_status(c, today, current)})")
        else:
            rem.append(f"{c.name}: {fmt(c.net)} spent (no limit)")
    if current:
        dues = due_commitments(store, today)
        if dues:
            rem.append("Bills still to pay: " + ", ".join(
                f"{d.name} {fmt(d.amount)} ({'overdue since' if d.overdue else 'due'} {short_date(d.due)})"
                for d in dues))
        for g in goal_statuses(store, today):
            line = f"Goal {g.name}: target {fmt(g.target)}"
            if g.progress is not None:
                line += f", {fmt(g.progress)} saved"
            if g.monthly:
                line += f", {fmt(g.contributed_this_month or 0)} of {fmt(g.monthly)} put aside this month"
            rem.append(line)
        a = compute_allowance(store, today)
        rem.append("Free to spend today: " + " ".join(allowance_lines(a)))
    if len(rem) == 1:
        rem.append("Nothing spent yet.")
    out.append("\n".join(rem))

    # 4. Patterns
    pats = patterns(s, store, today, current)
    out.append("PATTERNS\n" + ("\n".join(pats) if pats else "Not enough entries yet to spot patterns."))

    # 5. Actions
    out.append("WHAT TO DO\n" + "\n".join(recommendations(store, s, today, current)))
    return "\n\n".join(out)


def recommendations(store: Store, s: MonthSummary, today: date, current: bool) -> list[str]:
    recs: list[str] = []
    days_left = (s.end - today).days + 1 if current else 0
    for c in sorted((c for c in s.categories.values() if c.limit and is_consumption(c.kind)),
                    key=lambda c: c.pct or 0, reverse=True):
        if c.net > c.limit:
            recs.append(f"Stop discretionary spending in {c.name}; it is {fmt(c.net - c.limit)} over.")
        elif current and projected(c.net, today) > c.limit and days_left:
            recs.append(f"Hold {c.name} to {fmt(max(0, c.remaining) // days_left)}/day for the next "
                        f"{days_left} days.")
        if len(recs) >= 2:
            break
    subs = s.categories.get(store.resolve_category("Bills and subscriptions")["id"]) \
        if store.resolve_category("Bills and subscriptions") else None
    if subs and subs.count >= 3:
        recs.append(f"Review subscriptions: {subs.count} bill/subscription payments this month "
                    f"({fmt(subs.net)}).")
    if current:
        dues = due_commitments(store, today)
        if dues:
            recs.append(f"Keep {fmt(sum(d.amount for d in dues))} aside for unpaid commitments.")
        for g in goal_statuses(store, today):
            if g.shortfall_this_month:
                recs.append(f"Allocate {fmt(g.shortfall_this_month)} to {g.name} to stay on plan this month.")
    if s.overall_limit is None:
        recs.append("Set an overall monthly limit so KRONA can calculate a daily allowance.")
    if not recs:
        recs.append("No corrective action needed. Keep logging every purchase.")
    return recs


# ----------------------------------------------------------------------------- follow-ups
def category_spending(store: Store, today: date, category_id: int | None, label: str | None) -> str:
    s = summarise_month(store, today, through=today)
    if category_id is None:
        if label:
            return f"No category matches '{label}'. Categories: " + ", ".join(
                c["name"] for c in store.categories()) + "."
        lines = [f"Spending by category, {today.strftime('%B')} to date:"]
        for c in s.categories.values():
            if c.net:
                lines.append(f"{c.name}: {fmt(c.net)}" + (f" of {fmt(c.limit)} ({c.pct}%)" if c.limit else ""))
        return "\n".join(lines) if len(lines) > 1 else "No spending recorded this month."
    c = s.categories[category_id]
    lines = [f"{c.name}, {today.strftime('%B')} to date: {fmt(c.net)} across {c.count} purchases"
             + (f", net of {fmt(c.refunds)} refunds" if c.refunds else "") + "."]
    if c.limit:
        lines.append(f"Limit {fmt(c.limit)}; {fmt(c.remaining)} left ({c.pct}% used, "
                     f"{elapsed_pct(today)}% of the month gone).")
    else:
        lines.append("No limit set for this category.")
    rows = [t for t in s.expenses if t["category_id"] == category_id]
    for t in sorted(rows, key=lambda t: t["total_cents"], reverse=True)[:3]:
        lines.append(f"  {t['description']} {fmt(t['total_cents'])} ({short_date(date.fromisoformat(t['effective_date']))})")
    return "\n".join(lines)


def compare_months(store: Store, today: date) -> str:
    cur = summarise_month(store, today, through=today)
    pm = previous_month(today)
    same_day = pm.replace(day=min(today.day, days_in_month(pm)))
    prev_to_date = summarise_month(store, pm, through=same_day)
    prev_full = summarise_month(store, pm)
    if prev_full.tx_count == 0:
        return (f"No transactions recorded for {pm.strftime('%B')}, so there is nothing to compare. "
                f"This month to date: {fmt(cur.net_spending)} spent, {fmt(cur.income)} income.")
    lines = [f"{today.strftime('%B')} to {short_date(today)} vs {pm.strftime('%B')} to {short_date(same_day)}:",
             f"Spending: {fmt(cur.net_spending)} vs {fmt(prev_to_date.net_spending)}",
             f"Income: {fmt(cur.income)} vs {fmt(prev_to_date.income)}",
             f"{pm.strftime('%B')} full month: {fmt(prev_full.net_spending)} spent."]
    deltas = []
    for cid, c in cur.categories.items():
        p = prev_to_date.categories.get(cid)
        d = c.net - (p.net if p else 0)
        if d and is_consumption(c.kind):
            deltas.append((abs(d), d, c.name))
    for _, d, name in sorted(deltas, reverse=True)[:3]:
        lines.append(f"{name}: {'+' if d > 0 else '-'}{fmt(abs(d))}")
    return "\n".join(lines)


def safe_today(store: Store, today: date) -> str:
    return "FREE TO SPEND TODAY\n" + "\n".join(allowance_lines(compute_allowance(store, today)))


def overspending_facts(store: Store, today: date) -> list[str]:
    s = summarise_month(store, today, through=today)
    facts = [f"{elapsed_pct(today)}% of the month has passed (day {today.day} of {days_in_month(today)})."]
    if s.overall_limit:
        facts.append(f"Overall: {fmt(s.net_spending)} spent of {fmt(s.overall_limit)} ({s.overall_pct}%).")
    else:
        facts.append(f"Overall: {fmt(s.net_spending)} spent; no overall limit is set.")
    flagged = False
    for c in sorted(s.categories.values(), key=lambda c: c.net, reverse=True):
        if not is_consumption(c.kind) or c.net <= 0:
            continue
        if c.limit and (c.pct or 0) > elapsed_pct(today):
            flagged = True
            rows = sorted((t for t in s.expenses if t["category_id"] == c.id),
                          key=lambda t: t["total_cents"], reverse=True)[:2]
            drivers = ", ".join(f"{t['description']} {fmt(t['total_cents'])}" for t in rows)
            facts.append(f"{c.name}: {fmt(c.net)} of {fmt(c.limit)} ({c.pct}%), ahead of pace. Biggest: {drivers}.")
    if not flagged:
        facts.append("No limited category is ahead of pace.")
    facts.extend(p for p in patterns(s, store, today, True) if p.startswith(("Repeated", "Large")))
    return facts


def guarded_explanation(text: str, facts: list[str]) -> bool:
    """True if every number Claude used appears in the verified facts."""
    allowed = numbers_in_text("\n".join(facts))
    return numbers_in_text(text) <= allowed
