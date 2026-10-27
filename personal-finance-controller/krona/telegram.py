"""Minimal Telegram Bot API client over the standard library (no third-party dependency)."""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from typing import Protocol

from .fmt import split_message

log = logging.getLogger("krona.telegram")


class TelegramSendError(RuntimeError):
    """Sending failed. `uncertain` is True when Telegram may have delivered the message anyway."""

    def __init__(self, message: str, uncertain: bool):
        super().__init__(message)
        self.uncertain = uncertain


class Sender(Protocol):
    def send(self, chat_id: int, text: str, keyboard: dict | None = None) -> int: ...


class TelegramClient:
    def __init__(self, token: str, timeout_s: float = 30.0, api_root: str = "https://api.telegram.org"):
        self._base = f"{api_root}/bot{token}/"
        self.timeout_s = timeout_s

    def _post(self, method: str, payload: dict, timeout: float) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self._base + method, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
            body = json.loads(resp.read().decode())
        if not body.get("ok"):
            raise urllib.error.HTTPError("telegram", 400, str(body.get("description", "error"))[:200], None, None)
        return body["result"]

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        payload = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self._post("getUpdates", payload, timeout + 10)

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        """Register the bot's '/' menu so commands are discoverable in the Telegram app."""
        self._post("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]},
                   self.timeout_s)

    def _send_one(self, chat_id: int, text: str, keyboard: dict | None = None) -> int:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        try:
            res = self._post("sendMessage", payload, self.timeout_s)
            return int(res["message_id"])
        except urllib.error.HTTPError as exc:
            # Telegram answered: the message was not delivered (5xx gateway errors are ambiguous).
            raise TelegramSendError(f"Telegram rejected the message (HTTP {exc.code})",
                                    uncertain=exc.code in (502, 504)) from None
        except urllib.error.URLError as exc:
            # Connection could not be established: nothing was sent.
            reason = exc.reason
            uncertain = isinstance(reason, (socket.timeout, TimeoutError))
            raise TelegramSendError(f"Telegram unreachable ({type(reason).__name__})", uncertain=uncertain) from None
        except (socket.timeout, TimeoutError, ConnectionResetError):
            # Request may have reached Telegram before the response was lost.
            raise TelegramSendError("Telegram response timed out", uncertain=True) from None

    def send(self, chat_id: int, text: str, keyboard: dict | None = None) -> int:
        """Send text, split into parts if needed; the keyboard rides on the last part. Returns its message id."""
        parts = split_message(text)
        last = 0
        for i, part in enumerate(parts):
            try:
                last = self._send_one(chat_id, part, keyboard if i == len(parts) - 1 else None)
            except TelegramSendError as exc:
                if i > 0:
                    raise TelegramSendError(str(exc), uncertain=True) from None
                raise
        return last
