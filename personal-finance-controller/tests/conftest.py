"""Shared test harness. Claude and Telegram are mocked; SQLite is a real file in a temp dir."""

from __future__ import annotations

import itertools
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from krona.bot import Krona  # noqa: E402
from krona.claude import InterpreterUnavailable  # noqa: E402
from krona.db import open_db  # noqa: E402
from krona.store import Store  # noqa: E402
from krona.telegram import TelegramSendError  # noqa: E402
from krona.timeutil import SGT, fixed_clock  # noqa: E402

USER = 111111
STRANGER = 999999


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.fail_next: list[bool | None] = []  # None = success, False = definite failure, True = uncertain
        self._ids = itertools.count(1000)

    def send(self, chat_id: int, text: str) -> int:
        if self.fail_next:
            mode = self.fail_next.pop(0)
            if mode is not None:
                raise TelegramSendError("simulated", uncertain=mode)
        self.sent.append((chat_id, text))
        return next(self._ids)

    @property
    def last(self) -> str:
        return self.sent[-1][1]


class FakeInterpreter:
    """Returns queued raw intents; can simulate timeouts and malformed output."""

    def __init__(self):
        self.queue: list = []
        self.calls: list[tuple[str, dict]] = []
        self.explain_reply: str | Exception | None = None

    def push(self, item):
        self.queue.append(item)

    def interpret(self, text, context):
        self.calls.append((text, context))
        if not self.queue:
            raise InterpreterUnavailable("no scripted response")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def explain(self, question, facts):
        if isinstance(self.explain_reply, Exception):
            raise self.explain_reply
        return self.explain_reply or ""


class Harness:
    def __init__(self, db_path: Path, now: datetime, interpreter=None):
        self.db_path = db_path
        self.sender = FakeSender()
        self.interpreter = interpreter
        self.clock = fixed_clock(now)
        self._uid = itertools.count(1)
        self.open()

    def open(self):
        self.conn = open_db(self.db_path)
        self.bot = Krona(self.conn, USER, self.sender, self.interpreter, self.clock)
        self.store = Store(self.conn, USER)

    def restart(self):
        """Simulate a process restart: close the connection and reopen from disk."""
        self.conn.close()
        self.open()

    def set_now(self, dt: datetime):
        self.clock.holder["dt"] = dt

    def update(self, text: str, user: int = USER, update_id: int | None = None) -> dict:
        uid = update_id if update_id is not None else next(self._uid)
        return {"update_id": uid, "message": {"message_id": uid, "from": {"id": user},
                                               "chat": {"id": user, "type": "private"}, "text": text}}

    def say(self, text: str, **kw) -> str:
        before = len(self.sender.sent)
        self.bot.process_update(self.update(text, **kw))
        return self.sender.sent[-1][1] if len(self.sender.sent) > before else ""

    def tx_count(self, status: str | None = None) -> int:
        q = "SELECT COUNT(*) AS n FROM transactions" + (" WHERE status = ?" if status else "")
        return self.conn.execute(q, (status,) if status else ()).fetchone()["n"]


def sgt(y, m, d, hh=12, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=SGT)


@pytest.fixture
def h(tmp_path):
    """Harness without Claude (deterministic fallback parser only), 10 Oct 2026 12:00 SGT."""
    return Harness(tmp_path / "krona.sqlite3", sgt(2026, 10, 10))


@pytest.fixture
def hc(tmp_path):
    """Harness with a scripted (mock) Claude interpreter."""
    return Harness(tmp_path / "krona.sqlite3", sgt(2026, 10, 10), FakeInterpreter())


def tx(ttype, items, **kw):
    """Build a raw Claude-shaped transaction intent."""
    base = {"type": ttype, "items": items, "stated_total": None, "category": None, "date": None, "account": None,
            "from_account": None, "to_account": None, "income_kind": None, "refund_of": None}
    base.update(kw)
    return {"intent": "transaction", "transaction": base, "purchase": None, "query": None,
            "clarification_question": None, "reply": None}


def item(desc, qty=1, unit_price=None, line_total=None, us=None, up=None):
    return {"description": desc, "quantity": qty, "unit_singular": us, "unit_plural": up,
            "unit_price": unit_price, "line_total": line_total}
