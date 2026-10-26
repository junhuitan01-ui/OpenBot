"""Deterministic financial calculations. Claude never computes any figure here.

Definitions used throughout KRONA:
- Consumption spending: active expenses in categories of kind 'consumption' or 'uncategorised'.
- Net spending: consumption expenses minus active refunds in those categories.
- Savings outflows: expenses in the 'savings' category (allocations, not consumption).
- Transfers: movements between the user's own accounts. Never income, never spending.
- Net cash flow: income + refunds - all expenses (consumption and savings outflows). Transfers excluded.
- Income never raises a budget limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .money import fmt
from .store import Store
from .timeutil import days_in_month, month_bounds, period_key

LIQUID_KINDS = ("main", "spending", "cash")
DEFAULT_THRESHOLDS = (50, 75, 90, 100)


@dataclass
class CategoryUsage:
    id: int
    name: str
    kind: str
    gross: int = 0
    refunds: int = 0
    count: int = 0
    limit: int | None = None
    committed: int = 0  # unpaid commitments still due this period in this category

    @property
    def net(self) -> int:
        return self.gross - self.refunds

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else self.limit - self.net

    @property
    def pct(self) -> int | None:
        return None if not self.limit else (self.net * 100) // self.limit


@dataclass
class MonthSummary:
    start: date
    end: date
    through: date
    income: int = 0
    gross_spending: int = 0
    refunds: int = 0
    savings_outflows: int = 0
    transfers_total: int = 0
    transfers_to_savings: int = 0
    unlinked_refunds: int = 0
    overall_limit: int | None = None
    categories: dict[int, CategoryUsage] = field(default_factory=dict)
    expenses: list = field(default_factory=list)  # consumption expense rows (for pattern analysis)
    tx_count: int = 0
    reversed_count: int = 0
    corrected_count: int = 0

    @property
    def net_spending(self) -> int:
        return self.gross_spending - self.refunds

    @property
    def net_cash_flow(self) -> int:
        return self.income + self.refunds - self.gross_spending - self.savings_outflows

    @property
    def overall_remaining(self) -> int | None:
        return None if self.overall_limit is None else self.overall_limit - self.net_spending

    @property
    def overall_pct(self) -> int | None:
        return None if not self.overall_limit else (self.net_spending * 100) // self.overall_limit

    def by_name(self, name: str) -> CategoryUsage | None:
        for c in self.categories.values():
            if c.name.lower() == name.lower():
                return c
        return None


def is_consumption(kind: str | None) -> bool:
    return kind in ("consumption", "uncategorised")


def summarise_month(store: Store, any_day: date, through: date | None = None) -> MonthSummary:
    """Aggregate a calendar month (Singapore dates) from active ledger rows, optionally up to a date."""
    start, end = month_bounds(any_day)
    upto = min(end, through) if through else end
    s = MonthSummary(start=start, end=end, through=upto)
    limits = store.active_limits()
    s.overall_limit = limits.get(None)
    for c in store.categories(include_inactive=True):
        s.categories[c["id"]] = CategoryUsage(c["id"], c["name"], c["kind"], limit=limits.get(c["id"]))
    accounts = {a["id"]: a for a in store.accounts()}
    if upto >= start:
        rows = store.active_between(start, upto)
    else:
        rows = []
    for t in rows:
        s.tx_count += 1
        kind = t["category_kind"]
        cu = s.categories.get(t["category_id"]) if t["category_id"] else None
        amt = t["total_cents"]
        if t["type"] == "income":
            s.income += amt
        elif t["type"] == "expense":
            if is_consumption(kind):
                s.gross_spending += amt
                s.expenses.append(t)
            else:
                s.savings_outflows += amt
            if cu:
                cu.gross += amt
                cu.count += 1
        elif t["type"] == "refund":
            if is_consumption(kind):
                s.refunds += amt
            else:
                s.savings_outflows -= amt
            if cu:
                cu.refunds += amt
            if not t["related_transaction_id"]:
                s.unlinked_refunds += amt
        elif t["type"] == "transfer":
            s.transfers_total += amt
            dest = accounts.get(t["to_account_id"])
            if dest is not None and dest["kind"] in ("savings", "investment"):
                src = accounts.get(t["from_account_id"])
                if src is None or src["kind"] not in ("savings", "investment"):
                    s.transfers_to_savings += amt
    counts = store.status_counts_between(start, upto) if upto >= start else {}
    s.reversed_count = counts.get("reversed", 0)
    s.corrected_count = counts.get("corrected", 0)
    return s


# ---------------------------------------------------------------------- commitments
@dataclass
class DueCommitment:
    id: int
    name: str
    amount: int
    due: date
    category_id: int | None
    overdue: bool


def due_commitments(store: Store, today: date, period_end: date | None = None) -> list[DueCommitment]:
    """Unpaid commitments in today's month that are still owed (due later, or overdue and unpaid)."""
    start, end = month_bounds(today)
    end = period_end or end
    pk = period_key(today)
    out: list[DueCommitment] = []
    for c in store.commitments():
        if c["due_day"]:
            due = start.replace(day=min(c["due_day"], days_in_month(start)))
        else:
            due = date.fromisoformat(c["due_date"])
            if not (start <= due <= end):
                continue
        if due > end or store.commitment_paid(c["id"], pk):
            continue
        out.append(DueCommitment(c["id"], c["name"], c["amount_cents"], due, c["category_id"], due < today))
    return sorted(out, key=lambda d: d.due)


def commitments_by_category(store: Store, today: date, end: date) -> dict[int | None, int]:
    out: dict[int | None, int] = {}
    for d in due_commitments(store, today, end):
        out[d.category_id] = out.get(d.category_id, 0) + d.amount
    return out


# ---------------------------------------------------------------------- balances and goals
@dataclass
class Balances:
    accounts: list[tuple[str, str, int, str]]  # (name, kind, balance, as_of)
    liquid: int
    protected: int
    configured: bool


def balances(store: Store) -> Balances:
    rows = []
    liquid = 0
    for a in store.accounts():
        bal, anchor = store.account_balance(a["id"])
        rows.append((a["name"], a["kind"], bal, anchor["as_of_date"]))
        if a["kind"] in LIQUID_KINDS:
            liquid += bal
    protected = int(store.get_setting("protected_cents", "0") or 0)
    return Balances(rows, liquid, protected, bool(rows))


@dataclass
class GoalStatus:
    name: str
    target: int
    monthly: int | None
    contributed_this_month: int | None
    progress: int | None  # balance of linked account, if any
    shortfall_this_month: int


def goal_statuses(store: Store, today: date) -> list[GoalStatus]:
    start, end = month_bounds(today)
    out = []
    savings_cat_outflow = None
    for g in store.goals():
        progress = None
        contributed = None
        if g["account_id"]:
            progress, _ = store.account_balance(g["account_id"])
            contributed = store.transfers_into(g["account_id"], start, end)
        elif g["monthly_cents"]:
            if savings_cat_outflow is None:
                savings_cat_outflow = summarise_month(store, today).savings_outflows
            contributed = savings_cat_outflow
        shortfall = max(0, (g["monthly_cents"] or 0) - (contributed or 0)) if g["monthly_cents"] else 0
        out.append(GoalStatus(g["name"], g["target_cents"], g["monthly_cents"], contributed, progress, shortfall))
    return out


# ---------------------------------------------------------------------- daily allowance
@dataclass
class Allowance:
    status: str  # available | exhausted | provisional | unavailable
    amount: int | None  # recommended discretionary spend for today (start-of-day figure)
    spent_today: int
    days_left: int
    overall_left: int | None  # limit - spending before today - unpaid commitments
    committed: int
    cash_available: int | None
    binding: str | None  # which constraint set the figure: overall | cash
    exhausted_categories: list[str]
    tight_categories: list[tuple[str, int]]  # (name, per-day remaining) below the overall figure
    category_daily: list[tuple[str, int]]  # for provisional (category-only) budgets
    notes: list[str]

    @property
    def remaining_today(self) -> int | None:
        return None if self.amount is None else max(0, self.amount - self.spent_today)


def compute_allowance(store: Store, today: date) -> Allowance:
    """Recommended discretionary spend for `today`, recalculated from actual remaining money.

    remaining = overall limit - net consumption before today - unpaid commitments still due
    daily     = remaining // days left in the month (including today)
    If balances are maintained, the figure is also capped by liquid balance minus protected funds,
    commitments and this month's unmet savings allocations. Category limits never add money; they
    only restrict where it can go.
    """
    start, end = month_bounds(today)
    days_left = (end - today).days + 1
    before = summarise_month(store, today, through=today - timedelta(days=1)) if today > start else \
        summarise_month(store, today, through=start - timedelta(days=1))
    today_sum = summarise_month(store, today, through=today)
    spent_today = today_sum.net_spending - before.net_spending
    dues = due_commitments(store, today)
    committed = sum(d.amount for d in dues)
    reserved_by_cat: dict[int | None, int] = {}
    for d in dues:
        reserved_by_cat[d.category_id] = reserved_by_cat.get(d.category_id, 0) + d.amount
    notes: list[str] = []

    # Category restrictions use spending to date (including today), so they reflect what is left now.
    exhausted, tight, cat_daily = [], [], []
    for cu in today_sum.categories.values():
        if cu.limit is None or not is_consumption(cu.kind):
            continue
        left = cu.limit - cu.net - reserved_by_cat.get(cu.id, 0)
        if left <= 0:
            exhausted.append(cu.name)
        else:
            cat_daily.append((cu.name, left // days_left))

    bal = balances(store)
    cash_available = None
    if bal.configured:
        shortfall = sum(g.shortfall_this_month for g in goal_statuses(store, today))
        cash_available = bal.liquid - bal.protected - committed - shortfall
        if shortfall:
            notes.append(f"{fmt(shortfall)} kept aside for this month's savings allocations.")
        if bal.protected:
            notes.append(f"{fmt(bal.protected)} of protected funds excluded.")

    if before.overall_limit is None:
        status = "provisional" if cat_daily or exhausted else "unavailable"
        return Allowance(status, None, spent_today, days_left, None, committed, cash_available, None,
                         exhausted, [], cat_daily, notes)

    overall_left = before.overall_limit - before.net_spending - committed
    daily = max(0, overall_left) // days_left
    binding = "overall"
    if cash_available is not None:
        cash_daily = max(0, cash_available) // days_left
        if cash_daily < daily:
            daily, binding = cash_daily, "cash"
    tight = [(n, d) for n, d in cat_daily if d < daily]
    status = "available" if daily > 0 else "exhausted"
    return Allowance(status, daily, spent_today, days_left, overall_left, committed, cash_available, binding,
                     exhausted, tight, cat_daily, notes)


# ---------------------------------------------------------------------- pace
def elapsed_pct(today: date) -> int:
    return (today.day * 100) // days_in_month(today)


def projected(net: int, today: date) -> int:
    """Straight-line month-end projection from spending to date."""
    return (net * days_in_month(today)) // max(1, today.day)


def thresholds(store: Store) -> tuple[int, ...]:
    raw = store.get_setting("thresholds")
    if not raw:
        return DEFAULT_THRESHOLDS
    try:
        vals = tuple(sorted({int(x) for x in raw.split(",") if x.strip()}))
        return vals if vals and all(0 < v <= 200 for v in vals) else DEFAULT_THRESHOLDS
    except ValueError:
        return DEFAULT_THRESHOLDS
