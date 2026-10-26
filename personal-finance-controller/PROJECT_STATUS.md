# KRONA — Project status

Version 1.0.0 · last verified 2026-10-26

## Isolation

- KRONA lives only in `personal-finance-controller/`, with its own dependencies, `.env`, SQLite
  database (`data/krona.sqlite3`) and process. It requires its own Telegram bot token.
- The host repository (OpenBot) is a separate project. No file outside this directory was changed.
- No Daybreak project exists on the machine this was built on, so nothing of Daybreak's was read,
  started, stopped or modified.

## Implemented

| Area | Status |
|---|---|
| SQLite ledger: integer cents, forward-only migrations, WAL, atomic writes | Done |
| Natural-language logging via Claude structured outputs, strictly validated | Done |
| Strict local fallback parser for simple phrasings when Claude is unavailable | Done |
| Expenses, multiple items, quantities × unit prices, income (salary/bonus/allowance), refunds (linked when identifiable), transfers between own accounts, past dates | Done |
| Exact `Logged.` confirmation, sent only after commit; one concise warning line when warranted | Done |
| Conflicting totals, missing amounts, future dates, invented amounts or quantities → one clarifying question | Done |
| Hypothetical or question messages are never logged, even if the model says otherwise | Done |
| Idempotency per Telegram update; accidental-duplicate confirmation; separate identical purchases allowed | Done |
| `/history`, `/undo` (confirm, by id, or choose among matches), `/edit` (amount, price, quantity, description, category, date, per item) with audit events | Done |
| Default categories; add, rename, disable, enable, move (reassign entries) | Done |
| Category limits, overall limit, thresholds 50/75/90/100 with pace awareness, once per threshold per month | Done |
| Commitments (recurring day-of-month or one-off), reserved until paid; payday; cooling-off rule | Done |
| Manual accounts, opening balances, reconciliation, linked transfers; goals with monthly allocations; protected funds | Done |
| `should i buy` → BUY / WAIT / LET IT GO from deterministic rules; never creates a record | Done |
| `/report` (current, last, or a given month), split for Telegram; follow-ups (category spending, compare months, safe today, why overspending) | Done |
| "Why am I overspending" explanation from Claude, discarded if it uses any figure not in the verified facts | Done |
| Daily briefing 07:00 Asia/Singapore, catch-up until 12:00, persistent delivery records, backoff, marked resends | Done |
| Single authorised user, private chats only; secret redaction in logs; token-free Telegram errors | Done |
| `/start`, `/help`, `/report`, `/budget`, `/history`, `/undo`, `/edit`, `/accounts`, `/goals` | Done |

## Verified test results

Command: `.venv/bin/python -m pytest`, run on Python 3.13.

```
164 passed in 5.11s
```

Run repeatedly, and again in a fresh virtualenv installed from `requirements-dev.txt`; same
result every time. `ruff check krona tests`: all checks passed.

| File | Tests | Covers (spec section) |
|---|---|---|
| `test_money.py` | 21 | exact cents arithmetic, parsing SGD formats |
| `test_transactions.py` | 34 | 16.1: single, identical and distinct items, income, bonus, refund, reversal, transfer, missing amount, ambiguous, conflicting total, past date, hypothetical, malformed Claude output, Claude timeout, invented amounts |
| `test_calculations.py` | 15 | 16.2: totals, remaining, percentages, refunds, transfers excluded, SGT month boundaries, income not expanding budgets, missing config, no double counting, commitments excluded |
| `test_persistence.py` | 14 | 16.3: survives restart, same update twice, identical legitimate purchases, DB write failure, mid-write rollback, failure after commit, audited edit and reversal, report after edit, briefing record across restart |
| `test_decision.py` | 13 | 16.4: affordable, over category, over overall, recurring, missing data, no record created, no invented balances, cash shortfall and payday, cooling-off, Claude failure |
| `test_report.py` | 11 | 16.5: empty DB, income only, spending only, refunds and reversals, over budget, missing balances, current and previous months, reconciliation against rows, follow-ups, guarded explanation |
| `test_briefing.py` | 20 | 16.6: SGT timezone, previous day, midnight and month boundaries, no transactions, incomplete budgets, approaching and over limit, nothing left, commitments, refunds and reversals, deterministic, no duplicate, restart, catch-up, send failure, uncertain retry, crash mid-send, real scheduler thread |
| `test_security_telegram.py` | 24 | 16.7: every command, help lists only real commands, concise confirmation, alert spam, unauthorised and group rejection, reply failure retry, uncertain not resent, Telegram error classification, no token leak, log redaction, raw text not retained, single-instance lock |
| `test_acceptance.py` | 7 | 17 A–G end to end through `process_update` |
| `test_integration_local.py` | 5 | real Anthropic SDK request and response handling over a stub HTTP transport; real runtime loop (polling, offsets, restart dedupe, scheduler at 07:00 SGT) against a local fake Bot API server |

### What is mocked and what is live

- **Mocked:** Claude (scripted intents) and Telegram (`FakeSender`) in the unit and acceptance
  tests. In `test_integration_local.py` the real SDK and real HTTP client code run, but against
  local stubs.
- **Not yet run live:** no Anthropic API key or Telegram bot token exists in the build
  environment, and its network policy blocks `api.telegram.org`. So these have not been exercised
  end to end:
  1. A real Claude call. This includes whether the API accepts structured outputs combined with
     the `server-side-fallback-2026-07-01` beta. If it doesn't, set `KRONA_CLAUDE_FALLBACKS=off`.
     Any API error already degrades safely to the strict parser.
  2. Real Telegram polling and delivery, including an actual 07:00 SGT briefing.

  **Remaining step for you:** put your token, user ID and API key in `.env`, then run
  `python -m krona`. Send `/start`, then "Bought 2 bottles of protein milk at $3.80 each", then
  `/report`. Leave it running past 07:00 SGT to see the briefing.

## Known limitations

- **Host availability:** the briefing is sent only while the process is running and online. A
  missed 07:00 is caught up until 12:00 SGT. Use an always-on host for reliability (see README).
- **Uncertain deliveries:** the Bot API cannot confirm whether a timed-out message arrived. Retried
  briefings say "(Resent: ...)". Ordinary replies whose delivery is uncertain are not retried, to
  avoid a duplicate "Logged." message; `/history` always shows the truth.
- **Manual balances** change only through entries logged against an account. A reconciliation
  counts entries recorded after it with a date on or after the reconciliation date.
- **Refund linking** happens only when exactly one recent purchase matches. Otherwise the refund is
  recorded as unlinked, and the confirmation says which category it counts against.
- **Commitments** count as paid when an expense's description matches the commitment name, or on
  `/budget commit paid`. Until then they stay reserved, which is the conservative choice.
- **The fallback parser** without Claude understands only the phrasings listed in `/help`.
- **Single authorised user** by design. The schema is user-scoped, but only one user ID is admitted.
- **Python 3.10+ is declared; only 3.13 was tested here.** Windows needs the `tzdata` package (in
  `requirements.txt`).
