"""Claude intelligence layer: interprets messages into structured intent, and phrases explanations.

Claude never stores records and never produces authoritative figures. Every structured result is
validated by `krona.interpret` before it can touch the ledger.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

log = logging.getLogger("krona.claude")


class InterpreterUnavailable(RuntimeError):
    """Claude could not be reached, timed out, refused, or returned unusable output."""


class Interpreter(Protocol):
    def interpret(self, text: str, context: dict[str, Any]) -> dict[str, Any]: ...

    def explain(self, question: str, facts: list[str]) -> str: ...


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


_STR = {"type": "string"}
_ITEM = {
    "type": "object",
    "properties": {
        "description": _STR,
        "quantity": _nullable({"type": "integer"}),
        "unit_singular": _nullable(_STR),
        "unit_plural": _nullable(_STR),
        "unit_price": _nullable(_STR),
        "line_total": _nullable(_STR),
    },
    "required": ["description", "quantity", "unit_singular", "unit_plural", "unit_price", "line_total"],
    "additionalProperties": False,
}
INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["transaction", "purchase_question", "query", "clarify", "other"]},
        "transaction": _nullable({
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["expense", "income", "refund", "transfer"]},
                "items": {"type": "array", "items": _ITEM},
                "stated_total": _nullable(_STR),
                "category": _nullable(_STR),
                "date": _nullable({"type": "string", "format": "date"}),
                "account": _nullable(_STR),
                "from_account": _nullable(_STR),
                "to_account": _nullable(_STR),
                "income_kind": _nullable({"type": "string", "enum": ["salary", "bonus", "allowance", "other"]}),
                "refund_of": _nullable(_STR),
            },
            "required": ["type", "items", "stated_total", "category", "date", "account", "from_account",
                         "to_account", "income_kind", "refund_of"],
            "additionalProperties": False,
        }),
        "purchase": _nullable({
            "type": "object",
            "properties": {
                "item": _STR,
                "price": _nullable(_STR),
                "recurring": {"type": "string", "enum": ["none", "weekly", "monthly", "yearly"]},
                "category": _nullable(_STR),
                "need": {"type": "string", "enum": ["need", "want", "unknown"]},
                "asks_timing": {"type": "boolean"},
            },
            "required": ["item", "price", "recurring", "category", "need", "asks_timing"],
            "additionalProperties": False,
        }),
        "query": _nullable({
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["report", "category_spending", "compare_months", "safe_today",
                                                    "why_overspending", "history", "accounts", "budget"]},
                "category": _nullable(_STR),
            },
            "required": ["type", "category"],
            "additionalProperties": False,
        }),
        "clarification_question": _nullable(_STR),
        "reply": _nullable(_STR),
    },
    "required": ["intent", "transaction", "purchase", "query", "clarification_question", "reply"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are the language-understanding component of KRONA, a strict personal finance \
controller for one user in Singapore (currency SGD, timezone Asia/Singapore). You convert one Telegram \
message into a JSON intent. Application code stores records and does every calculation; you only extract \
what the user actually said.

Intents:
- "transaction": the user reports money that has ALREADY moved: an expense, income (pay, bonus, \
allowance), a refund received, or a transfer between their own accounts.
- "purchase_question": the user asks whether to buy, get or subscribe to something ("should i buy...", \
"can I afford..."). Never a transaction.
- "query": the user asks about their finances (report, spending in a category, compare months, how much \
they can safely spend today, why they are overspending, history, accounts, budget).
- "clarify": it looks like a transaction but a required detail is missing or contradictory. Put ONE short \
question in clarification_question, e.g. "What was the total amount paid?".
- "other": anything else (greetings, hypotheticals, plans, budget discussion). Put a one-sentence reply \
in reply that points to what KRONA can do. Hypothetical or future spending ("I might spend", "I'm going \
to buy", "if I spend") is NEVER a transaction.

Extraction rules for transactions:
- Copy amounts exactly as written, as plain decimal strings without currency symbols ("3.80", "900"). \
Never invent, estimate or compute a price that the user did not state. Never reuse amounts from earlier \
transactions.
- One item per distinct product. For "2 X at $3.80 each": quantity 2, unit_price "3.80", line_total null. \
For "X for $12": quantity 1, unit_price null, line_total "12". If the user states a total and also unit \
prices, put the total in stated_total. If no quantity is given, quantity 1.
- unit_singular/unit_plural: a natural container or unit if stated or obvious ("bottle"/"bottles", \
"cup"/"cups"); otherwise null. description: short, singular, capitalised item name without the unit \
("Meiji protein milk", "Lunch", "Internship pay").
- category: choose the best match from the provided category names, or null if genuinely unclear. \
Transfers and income have category null.
- date: the effective date as YYYY-MM-DD resolved against the provided today's date ("yesterday" = \
today minus one day). null when no date is mentioned.
- account / from_account / to_account: account names exactly as the user referred to them, else null. \
Transfers need both from_account and to_account; if one is missing, use "clarify".
- refund_of: for refunds, the purchase being refunded in a few words ("shoes"), else null.
- If the user describes several purchases in one message, list each as an item of one expense.
- If the message answers an earlier clarification question (provided in context), combine both to \
produce the complete intent.

All fields not relevant to the chosen intent must be null. Be conservative: when unsure whether money \
actually moved, do not output a transaction."""


class ClaudeInterpreter:
    """Anthropic API client wrapper. Raises InterpreterUnavailable on any failure."""

    def __init__(self, api_key: str, model: str, timeout_s: float = 20.0, use_fallbacks: bool = True):
        import anthropic  # imported lazily so deterministic features work without the SDK

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=1)
        self.model = model
        self.use_fallbacks = use_fallbacks

    def _create(self, **kwargs: Any):
        if self.use_fallbacks:
            return self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        return self.client.messages.create(**kwargs)

    def _call(self, **kwargs: Any) -> str:
        a = self._anthropic
        try:
            resp = self._create(model=self.model, **kwargs)
        except a.APITimeoutError as exc:
            raise InterpreterUnavailable("Claude timed out") from exc
        except a.RateLimitError as exc:
            raise InterpreterUnavailable("Claude rate limited") from exc
        except a.AuthenticationError as exc:
            raise InterpreterUnavailable("Claude authentication failed") from exc
        except a.APIStatusError as exc:
            raise InterpreterUnavailable(f"Claude API error {exc.status_code}") from exc
        except a.APIConnectionError as exc:
            raise InterpreterUnavailable("Claude unreachable") from exc
        if resp.stop_reason in ("refusal", "max_tokens"):
            raise InterpreterUnavailable(f"Claude stopped: {resp.stop_reason}")
        texts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        if not texts:
            raise InterpreterUnavailable("Claude returned no text")
        return "".join(texts)

    def interpret(self, text: str, context: dict[str, Any]) -> dict[str, Any]:
        user = "Context:\n" + json.dumps(context, ensure_ascii=False) + "\n\nMessage:\n" + text
        raw = self._call(
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": INTENT_SCHEMA}},
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InterpreterUnavailable("Claude returned malformed JSON") from exc
        if not isinstance(data, dict):
            raise InterpreterUnavailable("Claude returned a non-object")
        return data

    def explain(self, question: str, facts: list[str]) -> str:
        prompt = (
            "Answer the user's question in at most 4 short sentences, plain text, no greeting, no emojis. "
            "Use ONLY the verified facts below. Do not introduce any number, amount or percentage that is "
            "not in the facts. If the facts are insufficient, say what is missing.\n\nVerified facts:\n- "
            + "\n- ".join(facts) + f"\n\nQuestion: {question}"
        )
        return self._call(
            max_tokens=1500,
            system="You are KRONA: strict, direct, calm, financially analytical. Never invent figures.",
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low"},
        ).strip()
