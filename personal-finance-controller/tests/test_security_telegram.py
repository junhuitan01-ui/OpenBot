"""Spec 16.7 - commands, concise confirmations, duplicates, alert spam, auth, Telegram failures, secrets."""

import io
import logging
import socket
import urllib.error

import pytest

from krona.runtime import RedactingFilter
from krona.telegram import TelegramClient, TelegramSendError
from tests.conftest import STRANGER, FakeInterpreter, Harness, item, sgt, tx

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_xxxxxxxxxxxx"


def test_all_commands_respond_meaningfully(h):
    assert "Every dollar, accounted for." in h.say("/start")
    help_text = h.say("/help")
    for topic in ("/help budget", "/help accounts", "/help goals", "/help edit"):
        assert topic in help_text
        assert h.say(topic) != help_text  # each topic has its own page
    assert "TODAY · 10 Oct" in h.say("/today")
    assert "OVERVIEW" in h.say("/report")
    assert "BUDGET · October" in h.say("/budget")
    assert h.say("/history") == "Nothing logged yet. Type what you spent, like: lunch 12"
    assert h.say("/undo") == "Nothing to undo."
    assert h.say("/edit").startswith("Fix an entry:")
    assert "No accounts yet" in h.say("/accounts")
    assert "No savings goals yet" in h.say("/goals")
    assert h.say("/nonsense") == "I don't know that command. Tap Help to see what works."
    assert "OVERVIEW" in h.say("/report@krona_bot")


def test_help_lists_only_working_commands(h):
    import re
    from krona.bot import COMMANDS
    from krona.ui import COMMAND_MENU, HELP_TOPICS
    texts = [h.say("/help")] + list(HELP_TOPICS.values())
    listed = {c for t in texts for c in re.findall(r"/(\w+)", t)}
    assert listed <= set(COMMANDS)
    assert {c for c, _ in COMMAND_MENU} <= set(COMMANDS)


def test_budget_configuration_commands(h):
    assert h.say("/budget overall 1200").startswith("Monthly spending limit set to $1,200.00.")
    assert h.say("/budget set Food 400") == "Food and groceries: limit set to $400.00 a month."
    assert "isn't an amount I can use" in h.say("/budget set Food abc")
    assert "I don't have a category called" in h.say("/budget set Nonsense 10")
    assert h.say("/budget category add Pets") == "Category added: Pets."
    assert h.say("/budget category rename Pets to Pet care") == "Renamed Pets to Pet care."
    h.say("Spent $30 on widgets")
    assert h.say("/budget category move Uncategorised to Pet care") == "Moved 1 entry from Uncategorised to Pet care."
    assert h.say("/budget category disable Travel").startswith("Travel disabled.")
    assert "Travel [disabled]" in h.say("/budget categories")
    assert h.say("/budget clear Food") == "Food and groceries limit removed."
    assert "Commitment added: Rent $800.00 every month on day 1" in h.say("/budget commit add Rent 800 day 1")
    assert "Rent: $800.00, day 1 monthly" in h.say("/budget commit list")
    assert "marked paid" in h.say("/budget commit paid Rent")
    assert "Next expected income: 25 Oct" in h.say("/budget payday 2026-10-25")


def test_accounts_commands(h):
    assert h.say("/accounts add Main main 2500").startswith("Added Main (main) with $2,500.00.")
    h.say("/accounts add Wallet cash 50")
    from tests.conftest import item  # noqa: F401
    reply = h.say("/accounts reconcile Main 2410.50")
    assert reply == "Main reconciled to $2,410.50 (difference -$89.50 vs KRONA's figure)."
    out = h.say("/accounts")
    assert "not live bank data" in out and "Main: $2,410.50" in out


def test_expense_against_account_updates_manual_balance(hc):
    hc.say("/accounts add Main main 100")
    hc.interpreter.push(tx("expense", [item("Lunch", 1, None, "12")], category="Food", account="main account"))
    hc.say("Spent $12 on lunch from my main account")
    assert "Main: $88.00" in hc.say("/accounts")


def test_goals_commands(h):
    h.say("/accounts add Savings savings 1000")
    assert h.say("/goals add Emergency 5000 monthly 300 account Savings") == \
        "Goal added: Emergency, target $5,000.00, $300.00/month, tracked in Savings."
    assert "linked balance $1,000.00 (20%)" in h.say("/goals")
    assert "Protected funds: $2,000.00" in h.say("/goals protect 2000")
    assert h.say("/goals remove Emergency") == "Goal removed: Emergency."


def test_concise_logging_confirmation(h):
    reply = h.say("Spent $12 on lunch")
    assert reply.splitlines()[0] == "Logged." and len(reply.splitlines()) == 2


def test_alert_spam_prevention(h):
    h.say("/budget set Food 100")
    assert "Budget warning: Food and groceries is at 82%" in h.say("Spent $82 on groceries")
    assert h.say("Spent $3 on coffee") == "Logged.\nCoffee, 1 purchase, $3.00"  # 85%: same level, no repeat
    assert "Budget warning: Food and groceries is at 92%" in h.say("Spent $7 on snacks")
    assert "Budget exceeded: Food and groceries is at 112%" in h.say("Spent $20 on dinner")
    assert h.say("Spent $5 on milk") == "Logged.\nMilk, 1 purchase, $5.00"


def test_quiet_progress_note_only_when_ahead_of_pace(h):
    h.say("/budget set Food 100")
    assert "Budget note: Food and groceries is at 55%" in h.say("Spent $55 on groceries")  # day 10: 32% gone
    h2 = Harness(h.db_path.parent / "other.db", sgt(2026, 10, 25))
    h2.say("/budget set Food 100")
    assert h2.say("Spent $55 on groceries") == "Logged.\nGroceries, 1 purchase, $55.00"  # behind pace


def test_unauthorised_user_rejected_before_data_or_claude(tmp_path):
    fi = FakeInterpreter()
    hc = Harness(tmp_path / "k.db", sgt(2026, 10, 10), fi)
    hc.say("/budget overall 1000")
    for msg in ("/report", "Spent $12 on lunch", "/history", "should i buy shoes for $50?"):
        assert hc.say(msg, user=STRANGER) == "This is a private finance bot. Access denied."
    assert fi.calls == []
    assert hc.tx_count() == 0
    n = hc.conn.execute("SELECT COUNT(*) n FROM categories WHERE user_id = ?", (STRANGER,)).fetchone()["n"]
    assert n == 0


def test_group_chat_rejected(h):
    upd = h.update("/report")
    upd["message"]["chat"]["type"] = "group"
    h.bot.process_update(upd)
    assert h.sender.last == "This is a private finance bot. Access denied."


def test_reply_send_failure_is_retried_without_relogging(h):
    h.sender.fail_next = [False]
    h.bot.process_update(h.update("Spent $12 on lunch", update_id=77))
    assert h.tx_count() == 1 and h.sender.sent == []
    h.bot.retry_unsent()
    assert h.sender.last == "Logged.\nLunch, 1 purchase, $12.00"
    h.bot.retry_unsent()
    assert len(h.sender.sent) == 1


def test_uncertain_reply_not_resent(h):
    h.sender.fail_next = [True]
    h.bot.process_update(h.update("Spent $12 on lunch", update_id=78))
    h.bot.retry_unsent()
    h.bot.process_update(h.update("Spent $12 on lunch", update_id=78))  # Telegram redelivers the update
    assert h.sender.sent == [] and h.tx_count() == 1


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("exc,uncertain", [
    (urllib.error.HTTPError("u", 403, "Forbidden", None, None), False),
    (urllib.error.HTTPError("u", 502, "Bad Gateway", None, None), True),
    (urllib.error.URLError(ConnectionRefusedError()), False),
    (urllib.error.URLError(socket.gaierror()), False),
    (socket.timeout("timed out"), True),
])
def test_telegram_failure_classification_and_no_token_leak(monkeypatch, exc, uncertain):
    def fake_urlopen(req, timeout):
        raise exc
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = TelegramClient(FAKE_TOKEN)
    with pytest.raises(TelegramSendError) as ei:
        client.send(1, "hi")
    assert ei.value.uncertain is uncertain
    assert FAKE_TOKEN not in str(ei.value) and FAKE_TOKEN not in repr(ei.value)
    assert ei.value.__cause__ is None  # the URL (with token) is not chained into tracebacks


def test_telegram_success_returns_message_id(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: _Resp(b'{"ok": true, "result": {"message_id": 42}}'))
    assert TelegramClient(FAKE_TOKEN).send(1, "hi") == 42


def test_log_redaction_of_secrets():
    stream = io.StringIO()
    logger = logging.getLogger("krona.test.redact")
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter((FAKE_TOKEN, "sk-ant-secret-key-123")))
    logger.addHandler(handler)
    logger.propagate = False
    logger.warning("calling https://api.telegram.org/bot%s/sendMessage with %s", FAKE_TOKEN, "sk-ant-secret-key-123")
    try:
        raise RuntimeError(f"boom {FAKE_TOKEN}")
    except RuntimeError:
        logger.exception("failure")
    out = stream.getvalue()
    assert FAKE_TOKEN not in out and "sk-ant-secret-key-123" not in out and "<redacted>" in out


def test_settings_repr_hides_secrets(tmp_path, monkeypatch):
    from krona.config import from_env
    monkeypatch.setenv("KRONA_TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("KRONA_AUTHORISED_USER_ID", "42")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-key-123")
    monkeypatch.setenv("KRONA_DB_PATH", str(tmp_path / "x.db"))
    s = from_env(tmp_path)
    assert FAKE_TOKEN not in repr(s) and "sk-ant-secret-key-123" not in repr(s)


def test_db_error_reply_does_not_leak_internals(h, monkeypatch):
    import sqlite3
    from krona.store import Store

    def boom(*a, **k):
        raise sqlite3.OperationalError(f"no such table /secret/path {FAKE_TOKEN}")
    monkeypatch.setattr(Store, "commit_transaction", boom)
    reply = h.say("Spent $12 on lunch")
    assert FAKE_TOKEN not in reply and "/secret/path" not in reply


def test_raw_messages_not_retained(h):
    h.say("Spent $12 on lunch at a secret place")
    rows = h.conn.execute("SELECT response_text FROM processed_updates").fetchall()
    assert all(r["response_text"] is None for r in rows)  # cleared once delivered
    assert h.conn.execute("SELECT COUNT(*) n FROM pending_interactions").fetchone()["n"] == 0


def test_instance_lock_single_scheduler(tmp_path):
    from krona.runtime import InstanceLock
    a, b = InstanceLock(tmp_path / "k.db"), InstanceLock(tmp_path / "k.db")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()
