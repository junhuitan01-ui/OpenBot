"""Update processing: authorisation, idempotency, routing, and the transaction commit pipeline."""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from . import commands, report
from .alerts import check_after_commit
from .claude import Interpreter, InterpreterUnavailable
from .commands import Ctx
from .decision import evaluate
from .finance import due_commitments
from .fmt import confirmation, tx_line
from .interpret import (Clarify, Hypothetical, Malformed, Other, PurchaseQ, Query, Reject, TxPlan, build,
                        validate_raw)
from .money import MoneyError, fmt, numbers_in_text, parse_cents
from .parser import looks_hypothetical, parse, parse_query
from .store import Store, StoreError, TxDraft
from .telegram import Sender, TelegramSendError
from .timeutil import Clock, period_key

log = logging.getLogger("krona.bot")

YES = {"yes", "y", "yep", "yeah", "confirm", "ok", "okay", "log it", "yes log it"}
NO = {"no", "n", "nope", "cancel", "stop", "never mind", "nevermind", "don't", "dont"}
UNAUTHORISED_REPLY = "This is a private finance bot. Access denied."
DB_FAILURE_REPLY = "Not saved. The database could not record this, so nothing was logged. Please try again."
COMMANDS = ("start", "help", "report", "budget", "history", "undo", "edit", "accounts", "goals")


class Krona:
    def __init__(self, conn: sqlite3.Connection, authorised_user_id: int, sender: Sender,
                 interpreter: Interpreter | None, clock: Clock | None = None, briefing_hour: int = 7):
        self.conn = conn
        self.authorised_user_id = authorised_user_id
        self.sender = sender
        self.interpreter = interpreter
        self.clock = clock or Clock()
        self.briefing_hour = briefing_hour

    # ------------------------------------------------------------------ entry point
    def process_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        msg = update.get("message") or {}
        if not isinstance(update_id, int) or not isinstance(msg, dict):
            return
        sender_id = (msg.get("from") or {}).get("id")
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = msg.get("text")
        if not isinstance(chat_id, int):
            return
        # Authorisation happens before any financial data or Claude is touched.
        if sender_id != self.authorised_user_id or chat.get("type") not in (None, "private"):
            log.warning("rejected update from unauthorised sender")
            self._send_raw(chat_id, UNAUTHORISED_REPLY)
            return
        store = Store(self.conn, sender_id)
        store.ensure_user()
        prior = store.processed_update(update_id)
        if prior is not None:
            # Retry of an update we already handled: never redo side effects.
            if prior["reply_state"] in ("pending", "failed") and prior["response_text"]:
                self._deliver(store, update_id, chat_id, prior["response_text"])
            return
        if not isinstance(text, str) or not text.strip():
            store.record_processed(update_id, chat_id, None)
            return
        text = text.strip()[:2000]
        ctx = Ctx(store, self.clock.today(), update_id, chat_id, msg.get("message_id"), self.briefing_hour)
        try:
            reply = self.route(ctx, text)
        except sqlite3.Error:
            log.exception("database error while handling update")
            reply = self._recover_after_db_error(store, update_id)
        except StoreError as exc:
            reply = str(exc)
        try:
            if store.processed_update(update_id) is None:
                store.record_processed(update_id, chat_id, reply)
            else:
                store.set_response(update_id, reply)
        except sqlite3.Error:
            log.exception("could not record processed update; replying anyway")
            self._send_raw(chat_id, reply)
            return
        self._deliver(store, update_id, chat_id, reply)

    def _recover_after_db_error(self, store: Store, update_id: int) -> str:
        """Before claiming failure, check whether the write actually committed."""
        try:
            tx = store.transaction_for_update(update_id)
            prior = store.processed_update(update_id)
        except sqlite3.Error:
            return DB_FAILURE_REPLY
        if tx is not None and prior is not None and prior["response_text"]:
            return prior["response_text"]
        if tx is not None:
            return f"Logged.\n{tx_line(store, tx)}"
        return DB_FAILURE_REPLY

    # ------------------------------------------------------------------ sending
    def _send_raw(self, chat_id: int, text: str) -> None:
        try:
            self.sender.send(chat_id, text)
        except TelegramSendError as exc:
            log.warning("send failed: %s", exc)

    def _deliver(self, store: Store, update_id: int, chat_id: int, text: str) -> None:
        try:
            self.sender.send(chat_id, text)
        except TelegramSendError as exc:
            log.warning("reply delivery %s: %s", "uncertain" if exc.uncertain else "failed", exc)
            store.mark_reply(update_id, "uncertain" if exc.uncertain else "failed")
            return
        store.mark_reply(update_id, "sent")

    def retry_unsent(self) -> None:
        """Re-send replies that definitely failed earlier (never 'uncertain' ones, to avoid duplicates)."""
        store = Store(self.conn, self.authorised_user_id)
        for row in store.unsent_replies():
            self._deliver(store, row["update_id"], row["chat_id"], row["response_text"])

    # ------------------------------------------------------------------ routing
    def route(self, ctx: Ctx, text: str) -> str:
        if text.startswith("/"):
            ctx.store.clear_pending()
            return self.command(ctx, text)
        pending = ctx.store.get_pending()
        if pending:
            out = self.handle_pending(ctx, text, *pending)
            if out is not None:
                return out
        return self.natural(ctx, text)

    def command(self, ctx: Ctx, text: str) -> str:
        head, *args = text.split()
        name = head[1:].split("@", 1)[0].lower()
        if name not in COMMANDS:
            return "Unknown command. /help lists what works."
        if name == "start":
            return commands.start(ctx)
        if name == "help":
            return commands.HELP
        if name == "report":
            return commands.report_cmd(ctx, args)
        if name == "budget":
            return commands.budget(ctx, args)
        if name == "history":
            return commands.history(ctx, args)
        if name == "undo":
            return commands.undo(ctx, args)
        if name == "edit":
            return commands.edit(ctx, args)
        if name == "accounts":
            return commands.accounts(ctx, args)
        return commands.goals(ctx, args)

    def handle_pending(self, ctx: Ctx, text: str, kind: str, payload: dict) -> str | None:
        st = ctx.store
        low = text.lower().strip(" .!")
        if kind == "confirm_duplicate":
            if low in YES:
                st.clear_pending()
                return self.commit(ctx, TxDraft.from_json(payload["draft"]), skip_duplicate=True)
            if low in NO:
                st.clear_pending()
                return "Not logged."
            st.clear_pending()
            return None
        if kind == "confirm_undo":
            st.clear_pending()
            if low in YES:
                t = st.tx(payload["id"])
                if t is None or t["status"] != "active":
                    return "That entry is no longer active. Nothing changed."
                return commands.do_reverse(ctx, t["id"])
            if low in NO:
                return "Nothing changed."
            return None
        if kind == "choose_undo":
            st.clear_pending()
            if low.isdigit() and 1 <= int(low) <= len(payload["ids"]):
                t = st.tx(payload["ids"][int(low) - 1])
                if t is None or t["status"] != "active":
                    return "That entry is no longer active. Nothing changed."
                return commands.do_reverse(ctx, t["id"])
            if low in NO:
                return "Nothing changed."
            return None
        if kind == "clarify":
            st.clear_pending()
            if low in NO:
                return "Cancelled. Nothing was logged."
            combined = f"{payload['text']}\n(Answer to \"{payload['question']}\": {text})"
            return self.natural(ctx, combined, original=payload["text"], answer=text,
                                pending_question=payload["question"])
        st.clear_pending()
        return None

    # ------------------------------------------------------------------ natural language
    def _context(self, ctx: Ctx, pending_question: str | None) -> dict:
        t = ctx.today
        c = {"today": t.isoformat(), "weekday": t.strftime("%A"),
             "categories": [r["name"] for r in ctx.store.categories()],
             "accounts": [r["name"] for r in ctx.store.accounts()]}
        if pending_question:
            c["earlier_clarification_question"] = pending_question
        return c

    def interpret(self, ctx: Ctx, text: str, original: str | None = None, answer: str | None = None,
                  pending_question: str | None = None) -> tuple[Any, bool]:
        """Return (plan, used_fallback). Claude first; the strict local parser only if Claude fails."""
        claude_failed = False
        if self.interpreter is not None:
            try:
                raw = validate_raw(self.interpreter.interpret(text, self._context(ctx, pending_question)))
                if raw["intent"] in ("transaction", "clarify") and looks_hypothetical(answer or text):
                    # Deterministic backstop: a question or plan is never recorded, whatever Claude said.
                    log.warning("interpreter proposed a transaction for a hypothetical message; not logged")
                    return Hypothetical(), False
                return build(raw, ctx.store, text, ctx.today), False
            except InterpreterUnavailable as exc:
                log.warning("interpreter unavailable: %s", exc)
                claude_failed = True
            except Malformed as exc:
                log.warning("interpreter output rejected: %s", exc)
                claude_failed = True
        candidates = [text]
        if original is not None and answer is not None:
            candidates = [c for c in (_merge_amount_answer(original, answer), _append_price(original, answer),
                                      answer) if c]
        for local_text in candidates:
            raw = parse(local_text, ctx.today) or parse_query(local_text)
            if raw is not None:
                return build(validate_raw(raw), ctx.store, local_text, ctx.today), True
        return None, claude_failed

    def natural(self, ctx: Ctx, text: str, original: str | None = None, answer: str | None = None,
                pending_question: str | None = None) -> str:
        plan, degraded = self.interpret(ctx, text, original, answer, pending_question)
        if plan is None:
            if self.interpreter is None:
                return ("KRONA could not interpret that. Without the language service only simple phrasings work, "
                        "e.g. 'Spent $12 on lunch' or 'Received $900 internship pay'. Nothing was logged.")
            if degraded:
                return ("KRONA could not interpret that reliably right now (language service unavailable). "
                        "Nothing was logged. Retry shortly, or use a simple form: 'Spent $12 on lunch'.")
            return "KRONA could not interpret that. Nothing was logged."
        if isinstance(plan, Hypothetical):
            return ("That reads as a question or a plan, so nothing was logged. If you already paid, "
                    "say it plainly, e.g. 'Spent $40 on dinner'.")
        if isinstance(plan, Clarify):
            question = plan.question
            if plan.from_model and not _no_new_numbers(question, text):
                question = "What was the total amount paid?"
            ctx.store.set_pending("clarify", {"text": original or text, "question": question})
            return question
        if isinstance(plan, Reject):
            return plan.message
        if isinstance(plan, Other):
            if plan.reply and _no_new_numbers(plan.reply, text):
                return plan.reply
            return "I log spending and income, answer 'should i buy', and report with /report. /help lists examples."

        if isinstance(plan, PurchaseQ):
            return self.purchase(ctx, plan, original or text)
        if isinstance(plan, Query):
            return self.query(ctx, plan, text)
        assert isinstance(plan, TxPlan)
        return self.commit(ctx, plan.draft)

    # ------------------------------------------------------------------ handlers
    def commit(self, ctx: Ctx, draft: TxDraft, skip_duplicate: bool = False) -> str:
        st = ctx.store
        if not skip_duplicate:
            dup = st.find_recent_duplicate(draft)
            if dup is not None:
                st.set_pending("confirm_duplicate", {"draft": draft.to_json()}, minutes=15)
                return (f"This matches an entry logged moments ago: {tx_line(st, dup)}. "
                        "Log it again as a separate purchase? Reply yes or no.")
        commitment_ids = []
        if draft.type == "expense":
            desc = draft.description.lower()
            for dc in due_commitments(st, ctx.today):
                n = dc.name.lower()
                if (n in desc or desc in n) and draft.effective_date.strftime("%Y-%m") == period_key(ctx.today):
                    commitment_ids.append(dc.id)
                    draft.notes.append(f"Counted as this month's {dc.name} payment.")
        # Write first; the confirmation text exists only after a successful commit.
        tx_id = st.commit_transaction(draft, ctx.update_id, ctx.chat_id, ctx.message_id, "Logged.",
                                      commitment_ids, period_key(ctx.today))
        reply = confirmation(draft, tx_id, ctx.today)
        if draft.type in ("expense", "refund") and period_key(draft.effective_date) == period_key(ctx.today):
            try:
                warning = check_after_commit(st, draft.category_id, ctx.today) if draft.type == "expense" else None
            except sqlite3.Error:
                log.exception("budget check failed after commit")
                warning = None
            if warning:
                reply += "\n" + warning
        return reply

    def purchase(self, ctx: Ctx, p: PurchaseQ, text: str) -> str:
        if p.price_cents is None:
            ctx.store.set_pending("clarify", {"text": text, "question": "What is the price?"})
            return "What is the price?"
        cat = ctx.store.category(p.category_id) if p.category_id else None
        d = evaluate(ctx.store, ctx.today, p.price_cents, p.recurring, p.category_id, p.need, p.asks_timing)
        return d.render(p.item, p.price_cents, p.recurring, cat["name"] if cat else None)

    def query(self, ctx: Ctx, q: Query, text: str) -> str:
        if q.type == "report":
            return report.build_report(ctx.store, ctx.today)
        if q.type == "category_spending":
            return report.category_spending(ctx.store, ctx.today, q.category_id, q.category_text)
        if q.type == "compare_months":
            return report.compare_months(ctx.store, ctx.today)
        if q.type == "safe_today":
            return report.safe_today(ctx.store, ctx.today)
        if q.type == "history":
            return commands.history(ctx, [])
        if q.type == "accounts":
            return commands.accounts(ctx, [])
        if q.type == "budget":
            return commands.budget_overview(ctx)
        facts = report.overspending_facts(ctx.store, ctx.today)
        if self.interpreter is not None:
            try:
                answer = self.interpreter.explain(text, facts)
                if answer and report.guarded_explanation(answer, facts):
                    return answer
                log.warning("explanation discarded: it used figures not in the verified facts")
            except InterpreterUnavailable as exc:
                log.warning("explanation unavailable: %s", exc)
        return "\n".join(f"- {f}" for f in facts)


def _merge_amount_answer(original: str, answer: str) -> str | None:
    """Fallback-only: 'spent on lunch' + '$12' -> 'spent $12 on lunch'."""
    try:
        cents = parse_cents(answer.strip())
    except MoneyError:
        return None
    m = re.match(r"^\s*(?:i\s+)?(spent|paid)\s+(on|for)\s+(.+)$", original, re.I)
    if not m:
        return None
    return f"{m.group(1)} {fmt(cents)} {m.group(2)} {m.group(3)}"


def _append_price(original: str, answer: str) -> str | None:
    """Fallback-only: 'should i buy these shoes?' + '$120' -> 'should i buy these shoes for $120'."""
    try:
        cents = parse_cents(answer.strip())
    except MoneyError:
        return None
    return f"{original.rstrip(' ?')} for {fmt(cents)}?"


def _no_new_numbers(model_text: str, user_text: str) -> bool:
    """Model-written prose may not introduce figures the user never gave."""
    return numbers_in_text(model_text) <= numbers_in_text(user_text)
