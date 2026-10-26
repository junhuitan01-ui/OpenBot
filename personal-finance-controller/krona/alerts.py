"""Budget threshold warnings with spam prevention.

A warning is shown when a scope (a category or the overall limit) crosses a threshold it has not
already been warned about this month. Each threshold is announced at most once per month.
"""

from __future__ import annotations

from datetime import date

from .finance import elapsed_pct, summarise_month, thresholds
from .money import fmt
from .store import Store
from .timeutil import days_in_month, period_key


def _level(pct: int, levels: tuple[int, ...]) -> int:
    hit = [lv for lv in levels if pct >= lv]
    return max(hit) if hit else 0


def _phrase(name: str, pct: int, remaining: int, level: int, top: int, today: date, overall: bool) -> str:
    subject = "Overall spending" if overall else name
    limit_word = "the monthly limit" if overall else "its monthly limit"
    days_left = days_in_month(today) - today.day
    if level >= top and remaining <= 0:
        return f"{subject} is at {pct}% of {limit_word} ({fmt(-remaining)} over)."
    if level >= 90:
        return f"{subject} is at {pct}% of {limit_word}. {fmt(remaining)} remains for {days_left} days."
    return f"{subject} is at {pct}% of {limit_word}, with {elapsed_pct(today)}% of the month gone."


def check_after_commit(store: Store, category_id: int | None, today: date) -> str | None:
    """Return at most one line describing newly crossed thresholds, and record them."""
    s = summarise_month(store, today)
    levels = thresholds(store)
    top = max(levels)
    pk = period_key(today)
    found: list[tuple[int, str, str, bool]] = []  # (level, scope_key, phrase, quiet)
    scopes = []
    if category_id is not None and category_id in s.categories and s.categories[category_id].limit:
        cu = s.categories[category_id]
        scopes.append((f"cat:{cu.id}", cu.name, cu.net, cu.limit, False))
    if s.overall_limit:
        scopes.append(("overall", "Overall", s.net_spending, s.overall_limit, True))
    for key, name, net, limit, overall in scopes:
        pct = (net * 100) // limit
        lv = _level(pct, levels)
        if lv == 0 or lv <= store.alert_level(key, pk):
            continue
        quiet = lv == min(levels) and lv < 75
        if quiet and pct <= elapsed_pct(today):
            continue  # on or under pace: a 50% progress note would be noise
        found.append((lv, key, _phrase(name, pct, limit - net, lv, top, today, overall), quiet))
    if not found:
        return None
    for lv, key, _, _ in found:
        store.set_alert_level(key, pk, lv)
    found.sort(key=lambda f: f[0], reverse=True)
    lead_level, _, lead, quiet = found[0]
    if quiet and all(f[3] for f in found):
        prefix = "Budget note:"
    elif lead_level >= top and "over)" in lead:
        prefix = "Budget exceeded:"
    else:
        prefix = "Budget warning:"
    line = f"{prefix} {lead}"
    if len(found) > 1:
        second = found[1][2].rstrip(".")
        line += f" Also: {second[0].lower() + second[1:]}."
    return line
