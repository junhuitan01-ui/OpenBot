"""SQLite persistence: connection setup and forward-only migrations.

The database is never dropped or recreated on startup. Migrations run inside a
transaction and only ever add structure.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .timeutil import utc_now_iso

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE user_settings (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );

        CREATE TABLE categories (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('consumption', 'savings', 'uncategorised')),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX ux_categories_name ON categories (user_id, name COLLATE NOCASE);

        CREATE TABLE budgets (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            category_id INTEGER REFERENCES categories (id),
            limit_cents INTEGER NOT NULL CHECK (limit_cents > 0),
            period TEXT NOT NULL DEFAULT 'monthly' CHECK (period IN ('monthly')),
            start_date TEXT NOT NULL,
            end_date TEXT,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX ux_budgets_active ON budgets (user_id, IFNULL(category_id, 0)) WHERE active = 1;

        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('main', 'spending', 'savings', 'investment', 'cash', 'other')),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX ux_accounts_name ON accounts (user_id, name COLLATE NOCASE);

        -- Opening balances and later manual reconciliations. The latest row is the anchor.
        CREATE TABLE reconciliations (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL REFERENCES accounts (id),
            balance_cents INTEGER NOT NULL,
            as_of_date TEXT NOT NULL,
            is_opening INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            source_update_id INTEGER,
            source_message_id INTEGER,
            type TEXT NOT NULL CHECK (type IN ('expense', 'income', 'refund', 'transfer')),
            description TEXT NOT NULL,
            category_id INTEGER REFERENCES categories (id),
            total_cents INTEGER NOT NULL CHECK (total_cents > 0),
            currency TEXT NOT NULL DEFAULT 'SGD' CHECK (currency = 'SGD'),
            effective_date TEXT NOT NULL,
            account_id INTEGER REFERENCES accounts (id),
            from_account_id INTEGER REFERENCES accounts (id),
            to_account_id INTEGER REFERENCES accounts (id),
            related_transaction_id INTEGER REFERENCES transactions (id),
            income_kind TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'reversed', 'corrected')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (type != 'transfer' OR (from_account_id IS NOT NULL AND to_account_id IS NOT NULL
                                          AND from_account_id != to_account_id))
        );
        CREATE UNIQUE INDEX ux_tx_update ON transactions (user_id, source_update_id)
            WHERE source_update_id IS NOT NULL;
        CREATE INDEX ix_tx_user_date ON transactions (user_id, effective_date);

        CREATE TABLE transaction_items (
            id INTEGER PRIMARY KEY,
            transaction_id INTEGER NOT NULL REFERENCES transactions (id),
            line_no INTEGER NOT NULL,
            description TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            unit_singular TEXT,
            unit_plural TEXT,
            unit_price_cents INTEGER CHECK (unit_price_cents IS NULL OR unit_price_cents > 0),
            line_total_cents INTEGER NOT NULL CHECK (line_total_cents > 0),
            UNIQUE (transaction_id, line_no)
        );

        CREATE TABLE transaction_events (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            transaction_id INTEGER NOT NULL REFERENCES transactions (id),
            event TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );

        -- Idempotency: one row per Telegram update ever handled.
        CREATE TABLE processed_updates (
            update_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            chat_id INTEGER,
            response_text TEXT,
            reply_state TEXT NOT NULL DEFAULT 'pending'
                CHECK (reply_state IN ('pending', 'sent', 'failed', 'uncertain', 'none')),
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE pending_interactions (
            user_id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE budget_alerts (
            user_id INTEGER NOT NULL,
            scope_key TEXT NOT NULL,
            period TEXT NOT NULL,
            level INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, scope_key, period)
        );

        CREATE TABLE briefing_deliveries (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            briefing_date TEXT NOT NULL,
            briefing_type TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'failed', 'uncertain')),
            attempts INTEGER NOT NULL DEFAULT 0,
            telegram_message_id INTEGER,
            last_error TEXT,
            next_attempt_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            sent_at TEXT,
            UNIQUE (user_id, briefing_date, briefing_type)
        );

        CREATE TABLE goals (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            target_cents INTEGER NOT NULL CHECK (target_cents > 0),
            monthly_cents INTEGER CHECK (monthly_cents IS NULL OR monthly_cents > 0),
            account_id INTEGER REFERENCES accounts (id),
            target_date TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE commitments (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
            category_id INTEGER REFERENCES categories (id),
            due_day INTEGER CHECK (due_day IS NULL OR due_day BETWEEN 1 AND 31),
            due_date TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            CHECK ((due_day IS NULL) != (due_date IS NULL))
        );

        CREATE TABLE commitment_payments (
            commitment_id INTEGER NOT NULL REFERENCES commitments (id),
            period TEXT NOT NULL,
            transaction_id INTEGER REFERENCES transactions (id),
            created_at TEXT NOT NULL,
            PRIMARY KEY (commitment_id, period)
        );
        """,
    ),
]


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with safe defaults. Autocommit off; callers use `with conn:`."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Never drops data. Returns the resulting schema version."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    conn.commit()
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] or 0
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        # executescript commits implicitly; wrap statements manually for atomicity.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, utc_now_iso()))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        current = version
    return current


def _split_sql(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def open_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
