"""'should i buy' engine. The verdict is decided by deterministic rules over ledger data.

Verdicts: BUY, WAIT, LET IT GO. A decision never creates a transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .finance import compute_allowance, due_commitments, summarise_month
from .money import fmt
from .store import Store
from .timeutil import month_bounds, short_date

RECURRING_SHARE_LIMIT = 25  # % of the relevant monthly limit a new recurring cost may take before WAIT
LARGE_SHARE = 50  # % of remaining budget above which a one-off purchase warrants a cooling-off period


@dataclass
class Decision:
    verdict: str
    reasons: list[str]

    def render(self, item: str, price: int, recurring: str, category: str | None) -> str:
        cost = fmt(price) + ({"weekly": "/week", "monthly": "/month", "yearly": "/year"}.get(recurring, ""))
        head = f"{item}, {cost}" + (f" ({category})" if category else "")
        return "\n".join([self.verdict, head] + [f"- {r}" for r in self.reasons]
                         + ["Advice only. Nothing was logged."])


def evaluate(store: Store, today: date, price: int, recurring: str, category_id: int | None,
             need: str, asks_timing: bool) -> Decision:
    start, end = month_bounds(today)
    days_left = (end - today).days + 1
    s = summarise_month(store, today, through=today)
    dues = due_commitments(store, today)
    committed = sum(d.amount for d in dues)
    cat = s.categories.get(category_id) if category_id else None
    cat_reserved = sum(d.amount for d in dues if category_id and d.category_id == category_id)
    allowance = compute_allowance(store, today)

    if recurring == "weekly":
        monthly_equiv = price * 52 // 12
        this_month = price * ((days_left + 6) // 7)
    elif recurring == "yearly":
        monthly_equiv, this_month = price // 12, price
    elif recurring == "monthly":
        monthly_equiv, this_month = price, price
    else:
        monthly_equiv, this_month = 0, price

    reasons: list[str] = []
    cat_left = None
    if cat is not None and cat.limit:
        cat_left = cat.limit - cat.net - cat_reserved
        reasons.append(f"{cat.name} has {fmt(max(0, cat_left))} left this month"
                       + (f" after {fmt(cat_reserved)} committed" if cat_reserved else "") + ".")
    overall_left = None
    if s.overall_limit:
        overall_left = s.overall_limit - s.net_spending - committed
        reasons.append(f"Overall limit has {fmt(max(0, overall_left))} left for {days_left} days"
                       + (f" after {fmt(committed)} in commitments" if committed else "") + ".")
    cash_left = allowance.cash_available
    if cash_left is not None:
        reasons.append(f"Recorded balances leave {fmt(max(0, cash_left))} after protected funds, commitments "
                       "and savings allocations (user-maintained figures).")

    payday_raw = store.get_setting("next_income_date")
    payday = date.fromisoformat(payday_raw) if payday_raw else None
    if payday and payday < today:
        payday = None

    def payday_note() -> None:
        if payday:
            reasons.append(f"Next income expected {short_date(payday)}. Income does not raise your limits.")
        elif asks_timing:
            reasons.append("No expected income date is set (/budget payday YYYY-MM-DD).")

    if cat_left is None and overall_left is None and cash_left is None:
        reasons.append("No budget or balances are set, so KRONA cannot confirm this fits.")
        reasons.append("Conditional: buy only if it fits a limit you intend to keep and essentials are covered; "
                       "otherwise wait 24 hours. Set limits with /budget for a firm verdict.")
        if monthly_equiv:
            reasons.append(f"Recurring: {fmt(monthly_equiv)}/month, {fmt(monthly_equiv * 12)}/year.")
        return Decision("WAIT", reasons)

    over_cat = cat_left is not None and this_month > cat_left
    over_overall = overall_left is not None and this_month > overall_left
    over_cash = cash_left is not None and this_month > cash_left

    if over_cash:
        reasons.append(f"It exceeds the money available on record by {fmt(this_month - max(0, cash_left))}.")
        payday_note()
        return Decision("WAIT" if payday else "LET IT GO", reasons)

    if over_cat or over_overall:
        if over_cat:
            reasons.append(f"It exceeds {cat.name}'s remaining budget by {fmt(this_month - max(0, cat_left))}.")
        if over_overall:
            reasons.append(f"It exceeds the overall remaining budget by {fmt(this_month - max(0, overall_left))}.")
        fits_next = (cat is None or not cat.limit or this_month <= cat.limit) and \
                    (not s.overall_limit or this_month <= s.overall_limit)
        if recurring != "none":
            fits_next = fits_next and (not cat or not cat.limit or monthly_equiv * 100 <= cat.limit * RECURRING_SHARE_LIMIT)
        if fits_next:
            next_start = end + timedelta(days=1)
            reasons.append(f"It would fit next month's limits from 1 {next_start.strftime('%b')} if nothing else "
                           "is planned.")
            payday_note()
            return Decision("WAIT", reasons)
        reasons.append("It costs more than a full month's limit allows.")
        return Decision("LET IT GO", reasons)

    if recurring != "none":
        base = cat.limit if cat is not None and cat.limit else s.overall_limit
        reasons.append(f"Recurring: {fmt(monthly_equiv)}/month, {fmt(monthly_equiv * 12)}/year, every month.")
        if base and monthly_equiv * 100 > base * RECURRING_SHARE_LIMIT:
            reasons.append(f"That takes {monthly_equiv * 100 // base}% of the monthly limit permanently. "
                           "Cancel an existing subscription first or let it go.")
            return Decision("WAIT", reasons)

    remaining_ref = [v for v in (cat_left, overall_left, cash_left) if v is not None]
    tightest = min(remaining_ref)
    cool_raw = store.get_setting("cooloff_cents")
    cool_hours = store.get_setting("cooloff_hours", "48")
    if cool_raw and price >= int(cool_raw) and need != "need":
        reasons.append(f"Your cooling-off rule: wait {cool_hours} hours on discretionary purchases of "
                       f"{fmt(int(cool_raw))} or more.")
        return Decision("WAIT", reasons)
    if need != "need" and this_month * 100 > tightest * LARGE_SHARE and days_left > 3:
        reasons.append(f"It uses {this_month * 100 // max(1, tightest)}% of what is left with {days_left} days "
                       "to go. Wait 24 hours; buy only if you still want it.")
        payday_note()
        return Decision("WAIT", reasons)
    if need == "need":
        reasons.append("Stated as a need, and it fits.")
    else:
        reasons.append("It fits within your limits without crowding out the rest of the month.")
    return Decision("BUY", reasons)
