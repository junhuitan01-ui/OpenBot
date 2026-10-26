# KRONA

**Every dollar, accounted for.**

KRONA is a personal finance controller you run as a private Telegram bot. You tell it what you
spent and earned in plain language. It keeps an exact ledger in SQLite, enforces category limits
and an overall monthly limit, gives strict `should i buy` verdicts, produces a full `/report`, and
sends a compact briefing every morning at **07:00 Singapore time**.

Defaults: SGD, Asia/Singapore, variable income, manual accounts only, one authorised user.

## How it is built

| Responsibility | Where | Notes |
|---|---|---|
| Ledger and all arithmetic | `krona/store.py`, `krona/finance.py` | SQLite with integer cents. The only source of truth. |
| Interpreting messages | `krona/claude.py` (Claude), `krona/parser.py` (strict fallback) | Produce a structured intent and nothing else. |
| Validating interpreter output | `krona/interpret.py` | Rejects amounts and quantities that do not appear in your message, recomputes every total, and asks when totals conflict. |
| Budget warnings | `krona/alerts.py` | Thresholds 50/75/90/100%, each announced at most once per month. |
| Purchase verdicts | `krona/decision.py` | Deterministic BUY / WAIT / LET IT GO rules. |
| Reports and follow-ups | `krona/report.py` | |
| Daily briefing and scheduler | `krona/briefing.py` | Persistent delivery records, retries without silent duplicates. |
| Telegram | `krona/telegram.py`, `krona/bot.py`, `krona/commands.py` | Bot API over the standard library. Authorisation is checked before anything else runs. |
| Runtime | `krona/runtime.py` | Long polling, single-instance lock, secret redaction in logs. |

Claude never stores records and never produces a figure that KRONA displays as fact. If Claude is
unreachable, saved records, commands, reports and the briefing keep working. Simple messages such as
"Spent $12 on lunch" are still understood by the strict local parser; anything else gets "Nothing
was logged. Retry shortly."

## Setup

Requires Python 3.10 or newer. Tested on 3.13.

1. Create a **new** bot with [@BotFather](https://t.me/BotFather). KRONA must have its own token;
   never reuse another bot's.
2. Find your numeric Telegram user ID, for example by messaging @userinfobot.
3. Install:

   ```bash
   cd personal-finance-controller
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env    # then edit .env: token, your user ID, Anthropic API key
   ```

   Put secrets only in `.env`. Never paste them into a chat. `.env` and `data/` are git-ignored.

4. Start:

   ```bash
   .venv/bin/python -m krona
   ```

   The database is created at `data/krona.sqlite3` on first start. Later starts migrate the schema
   forward and never wipe data. A lock file next to the database stops a second copy from starting,
   so there is only ever one poller and one scheduler.

5. Open your bot in Telegram and send `/start`.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/ruff check krona tests
```

Claude and Telegram are mocked in the automated tests. See `PROJECT_STATUS.md` for exactly what
is and isn't verified against the live services.

## Using KRONA

### Logging

```
Spent $12 on lunch
Bought 2 bottles of protein milk at $3.80 each
Received $900 internship pay
Got $60 bonus from my shift
Received a $20 refund for shoes
Transferred $100 from my main account to savings
I spent $18.50 on dinner yesterday
```

A successful entry is confirmed only after it is committed to the database:

```
Logged.
Protein milk, 2 bottles, $7.60
```

A single warning line is added only when a threshold is newly crossed. If the amount is missing or
the numbers disagree, KRONA asks one question. Questions and plans ("I might spend $40...",
"should I...") are never logged.

### Budgets

```
/budget overall 1200            overall monthly limit on consumption spending
/budget set Food 400            category limit (short names like Food, Bills, Health work)
/budget clear Food
/budget categories
/budget category add Pets | rename Pets to Pet care | disable Travel | enable Travel
/budget category move Travel to Shopping      reassigns past entries, with an audit trail
/budget commit add Rent 800 day 1             recurring bill, reserved until paid
/budget commit add Course fee 300 on 2026-11-15
/budget commit paid Rent                      (also automatic when you log "Paid $800 for rent")
/budget payday 2026-11-10                     expected income date, used by "should i buy"
/budget cooloff 100 48                        wait 48h on discretionary purchases of $100+
```

You can set budgets gradually; logging works before anything is configured. Until a limit exists,
KRONA says so instead of inventing warnings or allowances. Rules:

- Monthly budgets follow calendar months in Singapore time.
- Income never raises a limit.
- Transfers between your own accounts are neither income nor spending.
- The "Savings and investments" category is reported as an allocation, not consumption.
- Refunds reduce the original purchase's category when KRONA can identify that purchase. Otherwise
  the refund is counted against a category, and the confirmation says which one.

### Accounts and goals (optional)

```
/accounts add Main main 2500        types: main, spending, savings, investment, cash, other
/accounts reconcile Main 2410.50
/goals add Emergency 5000 monthly 300 account Savings by 2027-06-30
/goals protect 2000                 never counted as spendable
```

Balances are maintained by you. They are not live bank data, and only entries logged against an
account change its balance. KRONA does not connect to any bank.

### Daily allowance ("free to spend today")

Calculated by `finance.compute_allowance`:

```
left  = overall limit - net consumption before today - unpaid commitments still due this month
daily = left / days remaining in the month (including today), rounded down to the cent
```

If you track accounts, the figure is also capped by your spendable balances. Protected funds,
unpaid commitments and this month's unmet goal allocations are subtracted first.

Category limits never add money. They only restrict where the money can go: an exhausted category
is listed as "spend nothing there". Unspent days do not accumulate; the figure is recalculated from
what is actually left. Without an overall limit, the allowance is reported as provisional or
unavailable rather than invented.

### Other commands

`/report` (also `/report last`, `/report 2026-09`), `/history` (`/history 20`, `/history all`),
`/undo` (last entry, with confirmation), `/undo 12`, `/undo lunch` (choose from matches),
`/edit 12 amount 15.20` (fields: amount, price, quantity, description, category, date; for
multi-item entries: `/edit 12 item 2 quantity 3`), `/help`.

Corrections never delete history. An edit marks the original `corrected` and creates a replacement;
an undo marks it `reversed`. Both are recorded in `transaction_events`.

Follow-up questions use the same engine: "Show food spending", "Compare this month to last month",
"How much can I safely spend today?", "Why am I overspending?".

## Daily briefing and host availability

The scheduler runs inside the bot process and sends the briefing at 07:00 Asia/Singapore. If the
process starts late, it sends the missed briefing until 12:00 SGT (`KRONA_BRIEFING_CATCHUP_UNTIL_HOUR`).
Every delivery is recorded in `briefing_deliveries`, keyed by date. It is marked sent only after
Telegram returns a message ID, so restarts and retries don't double-send.

Telegram cannot be asked whether a message arrived. When a send timed out and may have been
delivered, the retry carries a visible "(Resent: ...)" line, so you can always tell a duplicate apart.

**The host must be running and online at 07:00.** A laptop that is asleep or offline cannot send the
briefing, and messages sent to the bot while it is down are processed when it comes back (Telegram
keeps them for up to 24 hours). For reliable mornings, run KRONA on an always-on machine:

- Linux / Raspberry Pi / small VPS: `scripts/krona.service` (systemd user unit).
- macOS: `scripts/com.krona.bot.plist` (launchd agent), with sleep disabled or scheduled wake.
- Any host: keep the `data/` directory on persistent disk, and run only one instance per database.

## Privacy and security

- Only `KRONA_AUTHORISED_USER_ID`, in a private chat, is served. Everyone else is rejected before
  any data is read or Claude is called.
- Tokens and keys come from the environment or `.env`. Logs pass through a redaction filter, and
  Telegram errors are re-raised without the request URL, which contains the token.
- All SQL is parameterised, and every record is scoped by Telegram user ID.
- Raw messages are not stored. A pending clarification keeps the original text for up to 30
  minutes, and stored reply text is cleared once delivered.
