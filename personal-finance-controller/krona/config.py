"""Configuration from environment variables. Secrets never leave this module unredacted."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"


class ConfigError(RuntimeError):
    pass


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    authorised_user_id: int
    anthropic_api_key: str | None
    db_path: Path
    claude_model: str = DEFAULT_MODEL
    claude_timeout_s: float = 20.0
    claude_fallbacks: bool = True
    briefing_hour: int = 7
    briefing_catchup_until_hour: int = 12
    log_level: str = "INFO"
    secrets: tuple[str, ...] = field(default=(), repr=False)

    def __repr__(self) -> str:  # never print secrets
        return (
            f"Settings(authorised_user_id={self.authorised_user_id}, db_path={self.db_path}, "
            f"claude_model={self.claude_model}, telegram_token=<redacted>, anthropic_api_key="
            f"{'<redacted>' if self.anthropic_api_key else None})"
        )


def from_env(base_dir: Path | None = None) -> Settings:
    base = base_dir or Path(__file__).resolve().parent.parent
    load_dotenv(base / ".env")
    token = os.environ.get("KRONA_TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("KRONA_TELEGRAM_BOT_TOKEN is not set (see .env.example)")
    uid_raw = os.environ.get("KRONA_AUTHORISED_USER_ID", "").strip()
    if not uid_raw.isdigit():
        raise ConfigError("KRONA_AUTHORISED_USER_ID must be your numeric Telegram user ID")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or None
    db = Path(os.environ.get("KRONA_DB_PATH", "").strip() or (base / "data" / "krona.sqlite3"))
    hour = int(os.environ.get("KRONA_BRIEFING_HOUR", "7"))
    catchup = int(os.environ.get("KRONA_BRIEFING_CATCHUP_UNTIL_HOUR", "12"))
    if not (0 <= hour <= 23 and hour <= catchup <= 23):
        raise ConfigError("briefing hours must satisfy 0 <= KRONA_BRIEFING_HOUR <= catch-up hour <= 23")
    return Settings(
        telegram_token=token,
        authorised_user_id=int(uid_raw),
        anthropic_api_key=api_key,
        db_path=db,
        claude_model=os.environ.get("KRONA_CLAUDE_MODEL", "").strip() or DEFAULT_MODEL,
        claude_timeout_s=float(os.environ.get("KRONA_CLAUDE_TIMEOUT_SECONDS", "20")),
        claude_fallbacks=os.environ.get("KRONA_CLAUDE_FALLBACKS", "on").lower() not in ("off", "0", "false"),
        briefing_hour=hour,
        briefing_catchup_until_hour=catchup,
        log_level=os.environ.get("KRONA_LOG_LEVEL", "INFO").upper(),
        secrets=tuple(s for s in (token, api_key) if s),
    )
