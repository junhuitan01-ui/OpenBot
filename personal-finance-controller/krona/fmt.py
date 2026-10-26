"""Telegram text formatting for confirmations and history. Plain text only (no parse mode)."""

from __future__ import annotations

import sqlite3
from datetime import date

from .money import fmt
from .store import Store, TxDraft
from .timeutil import short_date

TELEGRAM_LIMIT = 4000


def qty_phrase(quantity: int, unit_singular: str | None, unit_plural: str | None) -> str:
    if unit_singular:
        return f"{quantity} {unit_singular if quantity == 1 else (unit_plural or unit_singular + 's')}"
    return "1 purchase" if quantity == 1 else f"{quantity} items"


def confirmation(d: TxDraft, tx_id: int, today: date) -> str:
    """The exact 'Logged.' confirmation. Only call after the commit succeeded."""
    lines = ["Logged."]
    if d.type == "expense":
        for it in d.items:
            lines.append(f"{it.description}, {qty_phrase(it.quantity, it.unit_singular, it.unit_plural)}, "
                         f"{fmt(it.line_total_cents)}")
        if len(d.items) > 1:
            lines.append(f"Total: {fmt(d.total_cents)}")
    elif d.type == "income":
        lines.append(f"{d.description}, income, {fmt(d.total_cents)}")
    elif d.type == "refund":
        lines.append(f"{d.description}, refund, {fmt(d.total_cents)}")
    else:
        lines.append(f"{d.description}, transfer, {fmt(d.total_cents)}")
    if d.effective_date != today:
        lines.append(f"Dated {short_date(d.effective_date)}.")
    lines.extend(n.replace("{id}", str(tx_id)) for n in d.notes)
    return "\n".join(lines)


def tx_line(store: Store, t: sqlite3.Row, show_status: bool = False) -> str:
    d = date.fromisoformat(t["effective_date"])
    if t["type"] == "expense":
        amount = f"-{fmt(t['total_cents'])}"
        label = (store.category(t["category_id"])["name"] if t["category_id"] else "Uncategorised")
    elif t["type"] == "income":
        amount, label = f"+{fmt(t['total_cents'])}", "income"
    elif t["type"] == "refund":
        amount, label = f"+{fmt(t['total_cents'])}", "refund"
    else:
        amount, label = fmt(t["total_cents"]), "transfer"
    line = f"#{t['id']} · {short_date(d)} · {t['description']} · {amount} · {label}"
    if show_status and t["status"] != "active":
        line += f" [{t['status']}]"
    return line


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on section boundaries (blank lines), then lines, to respect Telegram's size limit."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for block in text.split("\n\n"):
        candidate = f"{cur}\n\n{block}" if cur else block
        if len(candidate) <= limit:
            cur = candidate
            continue
        if cur:
            parts.append(cur)
        cur = ""
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        cur = block
    if cur:
        parts.append(cur)
    return parts
