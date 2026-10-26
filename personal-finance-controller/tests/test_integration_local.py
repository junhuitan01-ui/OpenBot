"""Local integration tests that exercise real code paths without external credentials.

- The real Anthropic SDK client, with HTTP answered by a stub transport (no network).
- The real runtime loop (long polling, offsets, scheduler thread, lock) against a local fake Bot API.
These are NOT live Telegram or live Claude tests; see PROJECT_STATUS.md.
"""

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2 as httpx
import pytest

from krona.claude import INTENT_SCHEMA, ClaudeInterpreter, InterpreterUnavailable
from krona.config import Settings
from krona.interpret import validate_raw
from krona.runtime import run
from krona.telegram import TelegramClient
from krona.timeutil import fixed_clock
from tests.conftest import USER, item, sgt, tx

KEY = "sk-ant-test-not-a-real-key"


def _message_response(text: str) -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10}}


def _interp(handler) -> tuple[ClaudeInterpreter, list]:
    import anthropic
    seen: list = []

    def wrapped(request: httpx.Request):
        seen.append(request)
        return handler(request)
    ci = ClaudeInterpreter(KEY, "claude-opus-5-5", 5.0, use_fallbacks=True)
    ci.client = anthropic.Anthropic(api_key=KEY, max_retries=0,
                                    http_client=httpx.Client(transport=httpx.MockTransport(wrapped)))
    return ci, seen


def test_real_sdk_request_shape_and_parse():
    intent = tx("expense", [item("Lunch", 1, None, "12")], category="Food and groceries")
    ci, seen = _interp(lambda r: httpx.Response(200, json=_message_response(json.dumps(intent))))
    out = ci.interpret("Spent $12 on lunch", {"today": "2026-10-10"})
    assert validate_raw(out)["transaction"]["items"][0]["line_total"] == "12"
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/messages"
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": INTENT_SCHEMA}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen[0].headers.get("anthropic-beta", "")
    assert "thinking" not in body and "temperature" not in body


def test_real_sdk_timeout_and_errors_map_to_unavailable():
    def timeout(_):
        raise httpx.ReadTimeout("timed out")
    for handler in (timeout,
                    lambda r: httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error",
                                                                                     "message": "x"}}),
                    lambda r: httpx.Response(200, json=_message_response("not json {")),
                    lambda r: httpx.Response(200, json={**_message_response(""), "stop_reason": "refusal",
                                                        "content": []})):
        ci, _ = _interp(handler)
        with pytest.raises(InterpreterUnavailable):
            ci.interpret("hello", {})


def test_schema_is_strict_everywhere():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(INTENT_SCHEMA)


# ----------------------------------------------------------------------- fake Bot API
class FakeBotAPI:
    def __init__(self, token: str):
        self.token = token
        self.pending: list[dict] = []
        self.sent: list[dict] = []
        self.offsets: list = []
        self.lock = threading.Lock()
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not self.path.startswith(f"/bot{api.token}/"):
                    return self._reply({"ok": False, "description": "Unauthorized"}, 401)
                method = self.path.rsplit("/", 1)[1]
                with api.lock:
                    if method == "getUpdates":
                        off = body.get("offset")
                        api.offsets.append(off)
                        if off is not None:
                            api.pending = [u for u in api.pending if u["update_id"] >= off]
                        return self._reply({"ok": True, "result": list(api.pending)})
                    if method == "sendMessage":
                        api.sent.append(body)
                        return self._reply({"ok": True, "result": {"message_id": len(api.sent)}})
                self._reply({"ok": False, "description": "unknown"}, 404)

            def _reply(self, payload, code=200):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.root = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def push(self, update_id: int, text: str, user: int = USER):
        with self.lock:
            self.pending.append({"update_id": update_id, "message": {
                "message_id": update_id, "from": {"id": user}, "chat": {"id": user, "type": "private"},
                "text": text}})


def _run_bot(tmp_path, api, clock, until, timeout_s=10.0):
    token = api.token
    settings = Settings(telegram_token=token, authorised_user_id=USER, anthropic_api_key=None,
                        db_path=tmp_path / "krona.sqlite3", secrets=(token,))
    stop = threading.Event()
    client = TelegramClient(token, timeout_s=5, api_root=api.root)
    t = threading.Thread(target=run, kwargs=dict(settings=settings, client=client, stop=stop, clock=clock,
                                                  poll_timeout=0), daemon=True)
    t.start()
    deadline = datetime.now(timezone.utc).timestamp() + timeout_s
    while not until() and datetime.now(timezone.utc).timestamp() < deadline:
        stop.wait(0.05)
    stop.set()
    t.join(timeout=10)
    return settings


def test_runtime_end_to_end_against_local_bot_api(tmp_path):
    api = FakeBotAPI("424242:LOCAL_TEST_TOKEN_abcdefghijklmnop")
    clock = fixed_clock(sgt(2026, 10, 10, 6, 0))  # before 07:00: no briefing in this run
    api.push(1, "/budget set Food 100")
    api.push(2, "Bought 2 bottles of protein milk at $3.80 each")
    api.push(3, "Spent $75 on groceries")
    api.push(4, "/report", user=999)  # stranger
    _run_bot(tmp_path, api, clock, until=lambda: len(api.sent) >= 4)
    texts = [m["text"] for m in api.sent]
    assert texts[1] == "Logged.\nProtein milk, 2 bottles, $7.60"
    assert texts[2].startswith("Logged.\nGroceries, 1 purchase, $75.00\nBudget warning: Food and groceries is at 82%")
    assert texts[3] == "This is a private finance bot. Access denied."
    # Restart with the same updates still on the server (e.g. offset never acknowledged): no duplicates.
    api.push(2, "Bought 2 bottles of protein milk at $3.80 each")
    api.push(5, "/history")
    _run_bot(tmp_path, api, clock, until=lambda: any("Recent entries" in m["text"] for m in api.sent))
    from krona.db import open_db
    conn = open_db(tmp_path / "krona.sqlite3")
    assert conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == 2
    assert sum(1 for m in api.sent if m["text"].startswith("Logged.\nProtein milk")) == 1


def test_runtime_scheduler_sends_briefing_at_0700_sgt(tmp_path):
    api = FakeBotAPI("424242:LOCAL_TEST_TOKEN_abcdefghijklmnop")
    clock = fixed_clock(datetime(2026, 10, 9, 23, 0, 30, tzinfo=timezone.utc))  # 07:00:30 SGT, 10 Oct
    _run_bot(tmp_path, api, clock, until=lambda: any("DAILY BRIEFING" in m["text"] for m in api.sent))
    briefs = [m for m in api.sent if "DAILY BRIEFING" in m["text"]]
    assert len(briefs) == 1 and "Saturday, 10 October 2026" in briefs[0]["text"]
    _run_bot(tmp_path, api, clock, until=lambda: False, timeout_s=1.5)  # restart within the window
    assert len([m for m in api.sent if "DAILY BRIEFING" in m["text"]]) == 1
