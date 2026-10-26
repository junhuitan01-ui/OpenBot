"""Process runtime: logging with secret redaction, single-instance lock, and the polling loop."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .bot import Krona
from .briefing import Scheduler
from .config import Settings
from .db import open_db
from .store import Store
from .telegram import TelegramClient
from .timeutil import Clock

log = logging.getLogger("krona")


class RedactingFilter(logging.Filter):
    """Removes configured secrets from every log record, including formatted args and tracebacks."""

    def __init__(self, secrets: tuple[str, ...]):
        super().__init__()
        self.secrets = tuple(s for s in secrets if s and len(s) >= 6)

    def _clean(self, s: str) -> str:
        for secret in self.secrets:
            s = s.replace(secret, "<redacted>")
        return s

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        record.msg = self._clean(msg)
        record.args = ()
        if record.exc_text:
            record.exc_text = self._clean(record.exc_text)
        return True


def setup_logging(level: str, secrets: tuple[str, ...]) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    # Third-party HTTP libraries can log request URLs; keep them quiet.
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class InstanceLock:
    """Exclusive lock next to the database so only one KRONA process (and one scheduler) runs."""

    def __init__(self, db_path: Path):
        self.path = Path(str(db_path) + ".lock")
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        return True

    def release(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def build_interpreter(settings: Settings):
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY not set: only simple phrasings will be understood")
        return None
    from .claude import ClaudeInterpreter
    return ClaudeInterpreter(settings.anthropic_api_key, settings.claude_model, settings.claude_timeout_s,
                             settings.claude_fallbacks)


def run(settings: Settings, *, client: TelegramClient | None = None, stop: threading.Event | None = None,
        clock: Clock | None = None, interpreter=None, poll_timeout: int = 25) -> int:
    setup_logging(settings.log_level, settings.secrets)
    lock = InstanceLock(settings.db_path)
    if not lock.acquire():
        log.error("another KRONA process is already running against %s; exiting", settings.db_path)
        return 2
    if stop is None:
        stop = threading.Event()

        def _stop(*_):
            stop.set()

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

    conn = open_db(settings.db_path)
    client = client or TelegramClient(settings.telegram_token)
    clock = clock or Clock()
    bot = Krona(conn, settings.authorised_user_id, client, interpreter or build_interpreter(settings), clock,
                settings.briefing_hour)

    def store_factory() -> Store:
        return Store(open_db(settings.db_path), settings.authorised_user_id)

    scheduler = Scheduler(store_factory, client, settings.authorised_user_id, clock, settings.briefing_hour,
                          settings.briefing_catchup_until_hour, stop)
    scheduler.start()
    state = Store(conn, settings.authorised_user_id)
    offset_raw = state.get_setting("telegram_offset")
    offset = int(offset_raw) if offset_raw else None
    log.info("KRONA running; database %s", settings.db_path)
    backoff = 1.0
    while not stop.is_set():
        try:
            bot.retry_unsent()
            updates = client.get_updates(offset, timeout=poll_timeout)
            backoff = 1.0
        except Exception as exc:  # network errors, Telegram outages
            log.warning("polling failed (%s); retrying in %.0fs", type(exc).__name__, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        for upd in updates:
            try:
                bot.process_update(upd)
            except Exception:
                log.exception("unhandled error processing update")
            offset = int(upd["update_id"]) + 1
            try:
                state.set_setting("telegram_offset", str(offset))
            except Exception:
                log.exception("could not persist polling offset")
    log.info("stopping")
    scheduler.join(timeout=5)
    conn.close()
    lock.release()
    return 0
