"""All database reads and writes. Every query is parameterised and scoped by user_id."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .timeutil import utc_now_iso

DEFAULT_CATEGORIES: list[tuple[str, str]] = [
    ("Food and groceries", "consumption"),
    ("Transport", "consumption"),
    ("Bills and subscriptions", "consumption"),
    ("Health and personal care", "consumption"),
    ("Shopping", "consumption"),
    ("Entertainment and social", "consumption"),
    ("Education and career", "consumption"),
    ("Travel", "consumption"),
    ("Gifts and miscellaneous", "consumption"),
    ("Savings and investments", "savings"),
    ("Uncategorised", "uncategorised"),
]

# Short words people actually type, mapped to default category names.
CATEGORY_ALIASES = {
    "food": "Food and groceries", "groceries": "Food and groceries", "grocery": "Food and groceries",
    "dining": "Food and groceries", "meals": "Food and groceries",
    "transport": "Transport", "transportation": "Transport",
    "bills": "Bills and subscriptions", "subscriptions": "Bills and subscriptions",
    "subscription": "Bills and subscriptions", "utilities": "Bills and subscriptions",
    "health": "Health and personal care", "personal care": "Health and personal care",
    "shopping": "Shopping",
    "entertainment": "Entertainment and social", "social": "Entertainment and social",
    "education": "Education and career", "career": "Education and career",
    "travel": "Travel",
    "gifts": "Gifts and miscellaneous", "gift": "Gifts and miscellaneous", "misc": "Gifts and miscellaneous",
    "miscellaneous": "Gifts and miscellaneous",
    "savings": "Savings and investments", "investments": "Savings and investments",
    "investment": "Savings and investments",
    "uncategorised": "Uncategorised", "uncategorized": "Uncategorised",
}

ACCOUNT_KINDS = ("main", "spending", "savings", "investment", "cash", "other")


class StoreError(RuntimeError):
    """A user-facing validation problem (not a database fault)."""


@dataclass
class ItemDraft:
    description: str
    quantity: int
    line_total_cents: int
    unit_price_cents: int | None = None
    unit_singular: str | None = None
    unit_plural: str | None = None


@dataclass
class TxDraft:
    type: str
    description: str
    items: list[ItemDraft]
    effective_date: date
    category_id: int | None = None
    account_id: int | None = None
    from_account_id: int | None = None
    to_account_id: int | None = None
    related_transaction_id: int | None = None
    income_kind: str | None = None
    notes: list[str] = field(default_factory=list)  # user-facing notes appended after confirmation

    @property
    def total_cents(self) -> int:
        return sum(i.line_total_cents for i in self.items)

    def to_json(self) -> str:
        d = self.__dict__.copy()
        d["effective_date"] = self.effective_date.isoformat()
        d["items"] = [i.__dict__ for i in self.items]
        return json.dumps(d)

    @classmethod
    def from_json(cls, s: str) -> "TxDraft":
        d = json.loads(s)
        d["effective_date"] = date.fromisoformat(d["effective_date"])
        d["items"] = [ItemDraft(**i) for i in d["items"]]
        return cls(**d)


class Store:
    def __init__(self, conn: sqlite3.Connection, user_id: int):
        self.conn = conn
        self.user_id = user_id

    # ------------------------------------------------------------------ users / settings
    def ensure_user(self) -> None:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM categories WHERE user_id = ?", (self.user_id,)).fetchone()
        if row["n"]:
            return
        now = utc_now_iso()
        with self.conn:
            for name, kind in DEFAULT_CATEGORIES:
                self.conn.execute(
                    "INSERT OR IGNORE INTO categories (user_id, name, kind, created_at, updated_at) VALUES (?,?,?,?,?)",
                    (self.user_id, name, kind, now, now),
                )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM user_settings WHERE user_id = ? AND key = ?", (self.user_id, key)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str | None) -> None:
        with self.conn:
            if value is None:
                self.conn.execute("DELETE FROM user_settings WHERE user_id = ? AND key = ?", (self.user_id, key))
            else:
                self.conn.execute(
                    "INSERT INTO user_settings (user_id, key, value, updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT (user_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (self.user_id, key, value, utc_now_iso()),
                )

    # ------------------------------------------------------------------ categories
    def categories(self, include_inactive: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM categories WHERE user_id = ?" + ("" if include_inactive else " AND active = 1")
        return self.conn.execute(sql + " ORDER BY id", (self.user_id,)).fetchall()

    def category(self, category_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM categories WHERE user_id = ? AND id = ?", (self.user_id, category_id)
        ).fetchone()

    def category_by_kind(self, kind: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM categories WHERE user_id = ? AND kind = ? ORDER BY id LIMIT 1", (self.user_id, kind)
        ).fetchone()
        if row is None:
            raise StoreError(f"no {kind} category configured")
        return row

    def resolve_category(self, name: str | None, include_inactive: bool = False) -> sqlite3.Row | None:
        """Match a user/Claude-supplied category name conservatively. None if not confident."""
        if not name:
            return None
        n = " ".join(name.strip().lower().replace("&", "and").split())
        cats = self.categories(include_inactive=include_inactive)
        for c in cats:
            if c["name"].lower() == n:
                return c
        alias = CATEGORY_ALIASES.get(n)
        if alias:
            for c in cats:
                if c["name"].lower() == alias.lower():
                    return c
        starts = [c for c in cats if c["name"].lower().startswith(n) and len(n) >= 3]
        if len(starts) == 1:
            return starts[0]
        first_word = [c for c in cats if c["name"].lower().split()[0] == n]
        if len(first_word) == 1:
            return first_word[0]
        return None

    def add_category(self, name: str) -> sqlite3.Row:
        name = " ".join(name.split())
        if not name or len(name) > 40:
            raise StoreError("Category names must be 1-40 characters.")
        existing = self.conn.execute(
            "SELECT * FROM categories WHERE user_id = ? AND name = ? COLLATE NOCASE", (self.user_id, name)
        ).fetchone()
        if existing:
            if existing["active"]:
                raise StoreError(f"{existing['name']} already exists.")
            with self.conn:
                self.conn.execute("UPDATE categories SET active = 1, updated_at = ? WHERE id = ?",
                                  (utc_now_iso(), existing["id"]))
            return self.category(existing["id"])
        now = utc_now_iso()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO categories (user_id, name, kind, created_at, updated_at) VALUES (?,?,?,?,?)",
                (self.user_id, name, "consumption", now, now),
            )
        return self.category(cur.lastrowid)

    def rename_category(self, category_id: int, new_name: str) -> None:
        new_name = " ".join(new_name.split())
        if not new_name or len(new_name) > 40:
            raise StoreError("Category names must be 1-40 characters.")
        clash = self.conn.execute(
            "SELECT id FROM categories WHERE user_id = ? AND name = ? COLLATE NOCASE AND id != ?",
            (self.user_id, new_name, category_id),
        ).fetchone()
        if clash:
            raise StoreError(f"A category named {new_name} already exists.")
        with self.conn:
            self.conn.execute("UPDATE categories SET name = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                              (new_name, utc_now_iso(), category_id, self.user_id))

    def set_category_active(self, category_id: int, active: bool) -> None:
        cat = self.category(category_id)
        if cat and cat["kind"] == "uncategorised" and not active:
            raise StoreError("Uncategorised cannot be disabled.")
        with self.conn:
            self.conn.execute("UPDATE categories SET active = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                              (1 if active else 0, utc_now_iso(), category_id, self.user_id))
            if not active:
                self.conn.execute(
                    "UPDATE budgets SET active = 0, end_date = date('now') WHERE user_id = ? AND category_id = ? AND active = 1",
                    (self.user_id, category_id),
                )

    def reassign_category(self, from_id: int, to_id: int) -> int:
        """Move every active transaction from one category to another, with an audit event each."""
        rows = self.conn.execute(
            "SELECT id FROM transactions WHERE user_id = ? AND category_id = ? AND status = 'active'",
            (self.user_id, from_id),
        ).fetchall()
        now = utc_now_iso()
        with self.conn:
            for r in rows:
                self.conn.execute("UPDATE transactions SET category_id = ?, updated_at = ? WHERE id = ?",
                                  (to_id, now, r["id"]))
                self._event(r["id"], "recategorised", {"from": from_id, "to": to_id}, now)
        return len(rows)

    # ------------------------------------------------------------------ budgets
    def active_limits(self) -> dict[int | None, int]:
        rows = self.conn.execute(
            "SELECT b.category_id, b.limit_cents FROM budgets b LEFT JOIN categories c ON c.id = b.category_id "
            "WHERE b.user_id = ? AND b.active = 1 AND (b.category_id IS NULL OR c.active = 1)",
            (self.user_id,),
        ).fetchall()
        return {r["category_id"]: r["limit_cents"] for r in rows}

    def set_limit(self, category_id: int | None, limit_cents: int | None, today: date) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE budgets SET active = 0, end_date = ? WHERE user_id = ? AND IFNULL(category_id, 0) = ? AND active = 1",
                (today.isoformat(), self.user_id, category_id or 0),
            )
            if limit_cents is not None:
                self.conn.execute(
                    "INSERT INTO budgets (user_id, category_id, limit_cents, start_date, created_at) VALUES (?,?,?,?,?)",
                    (self.user_id, category_id, limit_cents, today.isoformat(), utc_now_iso()),
                )

    # ------------------------------------------------------------------ alerts
    def alert_level(self, scope_key: str, period: str) -> int:
        row = self.conn.execute(
            "SELECT level FROM budget_alerts WHERE user_id = ? AND scope_key = ? AND period = ?",
            (self.user_id, scope_key, period),
        ).fetchone()
        return row["level"] if row else 0

    def set_alert_level(self, scope_key: str, period: str, level: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO budget_alerts (user_id, scope_key, period, level, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT (user_id, scope_key, period) DO UPDATE SET level = MAX(level, excluded.level)",
                (self.user_id, scope_key, period, level, utc_now_iso()),
            )

    # ------------------------------------------------------------------ accounts
    def accounts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM accounts WHERE user_id = ? AND active = 1 ORDER BY id", (self.user_id,)
        ).fetchall()

    def account(self, account_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM accounts WHERE user_id = ? AND id = ?", (self.user_id, account_id)
        ).fetchone()

    def resolve_account(self, name: str | None) -> sqlite3.Row | None:
        if not name:
            return None
        n = " ".join(name.strip().lower().split())
        for suffix in (" account", " acct", " a/c"):
            if n.endswith(suffix):
                n = n[: -len(suffix)].strip()
        if n.startswith("my "):
            n = n[3:]
        accts = self.accounts()
        exact = [a for a in accts if a["name"].lower() == n]
        if exact:
            return exact[0]
        by_kind = [a for a in accts if a["kind"] == n]
        if len(by_kind) == 1:
            return by_kind[0]
        prefix = [a for a in accts if a["name"].lower().startswith(n)]
        if len(prefix) == 1:
            return prefix[0]
        return None

    def add_account(self, name: str, kind: str, opening_cents: int, today: date) -> sqlite3.Row:
        name = " ".join(name.split())
        if not name or len(name) > 30:
            raise StoreError("Account names must be 1-30 characters.")
        if kind not in ACCOUNT_KINDS:
            raise StoreError(f"Account type must be one of: {', '.join(ACCOUNT_KINDS)}.")
        if self.conn.execute(
            "SELECT 1 FROM accounts WHERE user_id = ? AND name = ? COLLATE NOCASE", (self.user_id, name)
        ).fetchone():
            raise StoreError(f"An account named {name} already exists.")
        now = utc_now_iso()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO accounts (user_id, name, kind, created_at) VALUES (?,?,?,?)",
                (self.user_id, name, kind, now),
            )
            self.conn.execute(
                "INSERT INTO reconciliations (user_id, account_id, balance_cents, as_of_date, is_opening, created_at) "
                "VALUES (?,?,?,?,1,?)",
                (self.user_id, cur.lastrowid, opening_cents, today.isoformat(), now),
            )
        return self.account(cur.lastrowid)

    def reconcile(self, account_id: int, balance_cents: int, today: date) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO reconciliations (user_id, account_id, balance_cents, as_of_date, created_at) VALUES (?,?,?,?,?)",
                (self.user_id, account_id, balance_cents, today.isoformat(), utc_now_iso()),
            )

    def deactivate_account(self, account_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE accounts SET active = 0 WHERE id = ? AND user_id = ?", (account_id, self.user_id))

    def account_balance(self, account_id: int) -> tuple[int, sqlite3.Row]:
        """User-maintained balance: latest reconciliation plus logged movements recorded after it."""
        anchor = self.conn.execute(
            "SELECT * FROM reconciliations WHERE user_id = ? AND account_id = ? ORDER BY id DESC LIMIT 1",
            (self.user_id, account_id),
        ).fetchone()
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(CASE
                WHEN type = 'expense' AND account_id = :a THEN -total_cents
                WHEN type IN ('income', 'refund') AND account_id = :a THEN total_cents
                WHEN type = 'transfer' AND to_account_id = :a THEN total_cents
                WHEN type = 'transfer' AND from_account_id = :a THEN -total_cents
                ELSE 0 END), 0) AS delta
            FROM transactions
            WHERE user_id = :u AND status = 'active' AND created_at > :c AND effective_date >= :d
            """,
            {"a": account_id, "u": self.user_id, "c": anchor["created_at"], "d": anchor["as_of_date"]},
        ).fetchone()
        return anchor["balance_cents"] + row["delta"], anchor

    # ------------------------------------------------------------------ idempotency
    def processed_update(self, update_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM processed_updates WHERE update_id = ?", (update_id,)).fetchone()

    def _insert_processed(self, update_id: int, chat_id: int | None, response: str | None) -> None:
        now = utc_now_iso()
        self.conn.execute(
            "INSERT INTO processed_updates (update_id, user_id, chat_id, response_text, reply_state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (update_id, self.user_id, chat_id, response, "pending" if response else "none", now, now),
        )

    def record_processed(self, update_id: int, chat_id: int | None, response: str | None) -> None:
        with self.conn:
            self._insert_processed(update_id, chat_id, response)

    def set_response(self, update_id: int, response: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE processed_updates SET response_text = ?, updated_at = ? WHERE update_id = ?",
                (response, utc_now_iso(), update_id),
            )

    def mark_reply(self, update_id: int, state: str) -> None:
        # Once sent, drop the stored text: no need to retain financial replies.
        with self.conn:
            self.conn.execute(
                "UPDATE processed_updates SET reply_state = ?, attempts = attempts + 1, updated_at = ?, "
                "response_text = CASE WHEN ? IN ('sent', 'uncertain') THEN NULL ELSE response_text END "
                "WHERE update_id = ?",
                (state, utc_now_iso(), state, update_id),
            )

    def unsent_replies(self, max_attempts: int = 5) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM processed_updates WHERE user_id = ? AND reply_state IN ('pending', 'failed') "
            "AND response_text IS NOT NULL AND attempts < ? ORDER BY update_id",
            (self.user_id, max_attempts),
        ).fetchall()

    # ------------------------------------------------------------------ pending interactions
    def set_pending(self, kind: str, payload: dict, minutes: int = 30) -> None:
        now = datetime.now(timezone.utc)
        with self.conn:
            self.conn.execute(
                "INSERT INTO pending_interactions (user_id, kind, payload, created_at, expires_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT (user_id) DO UPDATE SET kind = excluded.kind, payload = excluded.payload, "
                "created_at = excluded.created_at, expires_at = excluded.expires_at",
                (self.user_id, kind, json.dumps(payload), now.isoformat(timespec="seconds"),
                 (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")),
            )

    def get_pending(self) -> tuple[str, dict] | None:
        row = self.conn.execute("SELECT * FROM pending_interactions WHERE user_id = ?", (self.user_id,)).fetchone()
        if row is None:
            return None
        if row["expires_at"] < datetime.now(timezone.utc).isoformat(timespec="seconds"):
            self.clear_pending()
            return None
        return row["kind"], json.loads(row["payload"])

    def clear_pending(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM pending_interactions WHERE user_id = ?", (self.user_id,))

    # ------------------------------------------------------------------ transactions
    def _event(self, tx_id: int, event: str, detail: dict | None, now: str) -> None:
        self.conn.execute(
            "INSERT INTO transaction_events (user_id, transaction_id, event, detail, created_at) VALUES (?,?,?,?,?)",
            (self.user_id, tx_id, event, json.dumps(detail) if detail else None, now),
        )

    def _insert_tx(self, d: TxDraft, update_id: int | None, message_id: int | None, now: str) -> int:
        if not d.items:
            raise StoreError("transaction has no lines")
        cur = self.conn.execute(
            """INSERT INTO transactions (user_id, source_update_id, source_message_id, type, description, category_id,
                   total_cents, effective_date, account_id, from_account_id, to_account_id, related_transaction_id,
                   income_kind, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)""",
            (self.user_id, update_id, message_id, d.type, d.description, d.category_id, d.total_cents,
             d.effective_date.isoformat(), d.account_id, d.from_account_id, d.to_account_id,
             d.related_transaction_id, d.income_kind, now, now),
        )
        tx_id = cur.lastrowid
        for n, it in enumerate(d.items, start=1):
            self.conn.execute(
                """INSERT INTO transaction_items (transaction_id, line_no, description, quantity, unit_singular,
                       unit_plural, unit_price_cents, line_total_cents) VALUES (?,?,?,?,?,?,?,?)""",
                (tx_id, n, it.description, it.quantity, it.unit_singular, it.unit_plural, it.unit_price_cents,
                 it.line_total_cents),
            )
        return tx_id

    def commit_transaction(self, d: TxDraft, update_id: int, chat_id: int | None, message_id: int | None,
                           response: str, commitment_ids: list[int] | None = None,
                           period: str | None = None) -> int:
        """Atomically write the transaction, its lines, audit event and idempotency record."""
        now = utc_now_iso()
        with self.conn:
            tx_id = self._insert_tx(d, update_id, message_id, now)
            self._event(tx_id, "created", None, now)
            for cid in commitment_ids or []:
                self.conn.execute(
                    "INSERT OR IGNORE INTO commitment_payments (commitment_id, period, transaction_id, created_at) "
                    "VALUES (?,?,?,?)", (cid, period, tx_id, now),
                )
            self._insert_processed(update_id, chat_id, response)
        return tx_id

    def transaction_for_update(self, update_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? AND source_update_id = ?", (self.user_id, update_id)
        ).fetchone()

    def tx(self, tx_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? AND id = ?", (self.user_id, tx_id)
        ).fetchone()

    def items(self, tx_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT i.* FROM transaction_items i JOIN transactions t ON t.id = i.transaction_id "
            "WHERE t.user_id = ? AND i.transaction_id = ? ORDER BY i.line_no", (self.user_id, tx_id)
        ).fetchall()

    def events(self, tx_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM transaction_events WHERE user_id = ? AND transaction_id = ? ORDER BY id",
            (self.user_id, tx_id),
        ).fetchall()

    def recent(self, limit: int = 10, include_inactive: bool = False) -> list[sqlite3.Row]:
        where = "" if include_inactive else " AND status = 'active'"
        return self.conn.execute(
            f"SELECT * FROM transactions WHERE user_id = ?{where} ORDER BY id DESC LIMIT ?", (self.user_id, limit)
        ).fetchall()

    def active_between(self, start: date, end: date) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT t.*, c.name AS category_name, c.kind AS category_kind FROM transactions t "
            "LEFT JOIN categories c ON c.id = t.category_id "
            "WHERE t.user_id = ? AND t.status = 'active' AND t.effective_date BETWEEN ? AND ? ORDER BY t.effective_date, t.id",
            (self.user_id, start.isoformat(), end.isoformat()),
        ).fetchall()

    def status_counts_between(self, start: date, end: date) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM transactions WHERE user_id = ? AND effective_date BETWEEN ? AND ? GROUP BY status",
            (self.user_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def find_recent_duplicate(self, d: TxDraft, within_minutes: int = 10) -> sqlite3.Row | None:
        since = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat(timespec="microseconds")
        return self.conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? AND status = 'active' AND type = ? AND total_cents = ? "
            "AND lower(description) = lower(?) AND effective_date = ? AND created_at >= ? ORDER BY id DESC LIMIT 1",
            (self.user_id, d.type, d.total_cents, d.description, d.effective_date.isoformat(), since),
        ).fetchone()

    def search_expenses(self, text: str, since: date) -> list[sqlite3.Row]:
        like = f"%{text.strip().lower()}%"
        return self.conn.execute(
            "SELECT DISTINCT t.* FROM transactions t LEFT JOIN transaction_items i ON i.transaction_id = t.id "
            "WHERE t.user_id = ? AND t.status = 'active' AND t.type = 'expense' AND t.effective_date >= ? "
            "AND (lower(t.description) LIKE ? OR lower(i.description) LIKE ?) ORDER BY t.id DESC",
            (self.user_id, since.isoformat(), like, like),
        ).fetchall()

    def search_active(self, text: str, limit: int = 10) -> list[sqlite3.Row]:
        like = f"%{text.strip().lower()}%"
        return self.conn.execute(
            "SELECT DISTINCT t.* FROM transactions t LEFT JOIN transaction_items i ON i.transaction_id = t.id "
            "WHERE t.user_id = ? AND t.status = 'active' AND (lower(t.description) LIKE ? OR lower(i.description) LIKE ?) "
            "ORDER BY t.id DESC LIMIT ?",
            (self.user_id, like, like, limit),
        ).fetchall()

    def refunded_total(self, original_id: int, exclude_id: int | None = None) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(total_cents), 0) AS s FROM transactions WHERE user_id = ? AND type = 'refund' "
            "AND status = 'active' AND related_transaction_id = ? AND id != ?",
            (self.user_id, original_id, exclude_id or 0),
        ).fetchone()
        return row["s"]

    def reverse(self, tx_id: int, update_id: int | None, chat_id: int | None, response: str | None) -> None:
        now = utc_now_iso()
        with self.conn:
            cur = self.conn.execute(
                "UPDATE transactions SET status = 'reversed', updated_at = ? WHERE id = ? AND user_id = ? AND status = 'active'",
                (now, tx_id, self.user_id),
            )
            if cur.rowcount != 1:
                raise StoreError("That transaction is no longer active.")
            self.conn.execute("DELETE FROM commitment_payments WHERE transaction_id = ?", (tx_id,))
            self._event(tx_id, "reversed", None, now)
            if update_id is not None:
                self._insert_processed(update_id, chat_id, response)

    def correct(self, tx_id: int, new: TxDraft, changes: dict, update_id: int | None, chat_id: int | None,
                response_factory) -> int:
        """Replace a transaction with a corrected copy. The original is kept with status 'corrected'."""
        now = utc_now_iso()
        with self.conn:
            cur = self.conn.execute(
                "UPDATE transactions SET status = 'corrected', updated_at = ? WHERE id = ? AND user_id = ? AND status = 'active'",
                (now, tx_id, self.user_id),
            )
            if cur.rowcount != 1:
                raise StoreError("That transaction is no longer active.")
            new.related_transaction_id = new.related_transaction_id if new.type == "refund" else tx_id
            new_id = self._insert_tx(new, None, None, now)
            self._event(tx_id, "corrected", {"replaced_by": new_id, "changes": changes}, now)
            self._event(new_id, "created", {"corrects": tx_id, "changes": changes}, now)
            # Linked refunds follow the corrected purchase.
            self.conn.execute(
                "UPDATE transactions SET related_transaction_id = ?, updated_at = ? WHERE user_id = ? AND type = 'refund' "
                "AND related_transaction_id = ? AND status = 'active'", (new_id, now, self.user_id, tx_id),
            )
            self.conn.execute("UPDATE commitment_payments SET transaction_id = ? WHERE transaction_id = ?", (new_id, tx_id))
            if update_id is not None:
                self._insert_processed(update_id, chat_id, response_factory(new_id))
        return new_id

    # ------------------------------------------------------------------ commitments
    def commitments(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM commitments WHERE user_id = ? AND active = 1 ORDER BY id", (self.user_id,)
        ).fetchall()

    def add_commitment(self, name: str, amount_cents: int, category_id: int | None, due_day: int | None,
                       due_date: date | None) -> int:
        name = " ".join(name.split())
        if not name or len(name) > 40:
            raise StoreError("Commitment names must be 1-40 characters.")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO commitments (user_id, name, amount_cents, category_id, due_day, due_date, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (self.user_id, name, amount_cents, category_id, due_day, due_date.isoformat() if due_date else None,
                 utc_now_iso()),
            )
        return cur.lastrowid

    def resolve_commitment(self, name: str) -> sqlite3.Row | None:
        n = name.strip().lower()
        rows = [c for c in self.commitments() if c["name"].lower() == n]
        if not rows:
            rows = [c for c in self.commitments() if c["name"].lower().startswith(n)]
        return rows[0] if len(rows) == 1 else None

    def deactivate_commitment(self, cid: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE commitments SET active = 0 WHERE id = ? AND user_id = ?", (cid, self.user_id))

    def commitment_paid(self, cid: int, period: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM commitment_payments WHERE commitment_id = ? AND period = ?", (cid, period)
        ).fetchone() is not None

    def mark_commitment_paid(self, cid: int, period: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO commitment_payments (commitment_id, period, created_at) VALUES (?,?,?)",
                (cid, period, utc_now_iso()),
            )

    # ------------------------------------------------------------------ goals
    def goals(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM goals WHERE user_id = ? AND active = 1 ORDER BY id", (self.user_id,)
        ).fetchall()

    def add_goal(self, name: str, target_cents: int, monthly_cents: int | None, account_id: int | None,
                 target_date: date | None) -> int:
        name = " ".join(name.split())
        if not name or len(name) > 40:
            raise StoreError("Goal names must be 1-40 characters.")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO goals (user_id, name, target_cents, monthly_cents, account_id, target_date, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (self.user_id, name, target_cents, monthly_cents, account_id,
                 target_date.isoformat() if target_date else None, utc_now_iso()),
            )
        return cur.lastrowid

    def deactivate_goal(self, name: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE goals SET active = 0 WHERE user_id = ? AND name = ? COLLATE NOCASE AND active = 1",
                (self.user_id, name.strip()),
            )
        return cur.rowcount > 0

    def transfers_into(self, account_id: int, start: date, end: date) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(total_cents), 0) AS s FROM transactions WHERE user_id = ? AND status = 'active' "
            "AND type = 'transfer' AND to_account_id = ? AND effective_date BETWEEN ? AND ?",
            (self.user_id, account_id, start.isoformat(), end.isoformat()),
        ).fetchone()
        return row["s"]

    # ------------------------------------------------------------------ briefings
    def briefing(self, d: date, btype: str = "daily") -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM briefing_deliveries WHERE user_id = ? AND briefing_date = ? AND briefing_type = ?",
            (self.user_id, d.isoformat(), btype),
        ).fetchone()

    def claim_briefing(self, d: date, btype: str = "daily") -> sqlite3.Row:
        """Insert or move the delivery record to 'sending' before contacting Telegram."""
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                "INSERT INTO briefing_deliveries (user_id, briefing_date, briefing_type, status, attempts, created_at, updated_at) "
                "VALUES (?,?,?,'sending',1,?,?) ON CONFLICT (user_id, briefing_date, briefing_type) DO UPDATE SET "
                "status = 'sending', attempts = attempts + 1, updated_at = excluded.updated_at "
                "WHERE briefing_deliveries.status != 'sent'",
                (self.user_id, d.isoformat(), btype, now, now),
            )
        return self.briefing(d, btype)

    def finish_briefing(self, d: date, status: str, *, message_id: int | None = None, error: str | None = None,
                        next_attempt_at: str | None = None, btype: str = "daily") -> None:
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                "UPDATE briefing_deliveries SET status = ?, telegram_message_id = COALESCE(?, telegram_message_id), "
                "last_error = ?, next_attempt_at = ?, updated_at = ?, sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END "
                "WHERE user_id = ? AND briefing_date = ? AND briefing_type = ? AND status != 'sent'",
                (status, message_id, error, next_attempt_at, now, status, now, self.user_id, d.isoformat(), btype),
            )
