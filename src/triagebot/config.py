"""Configuration, loaded from the environment in exactly one place.

Secrets live only in environment variables (or a gitignored .env), never in
source. The dataclass fields holding secrets are marked ``repr=False`` so that
logging or printing a Config can never leak a key -- an easy accident that is
hard to notice once it is in a log file.

Missing variables are reported *all at once* rather than one per run, so a
first-time setup takes one round trip instead of five.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_MODEL = "gpt-5-nano"
DEFAULT_WORKSHEET = "Inquiries"
DEFAULT_LOG_LEVEL = "INFO"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration."""

    # repr=False on every secret: a Config must be safe to log.
    openai_api_key: str = field(repr=False)
    openai_model: str
    openai_base_url: str | None

    google_service_account_file: Path
    google_sheet_id: str
    google_worksheet_name: str

    telegram_bot_token: str = field(repr=False)
    telegram_chat_id: str

    log_level: str = DEFAULT_LOG_LEVEL

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> Config:
        """Build a Config from the environment, loading .env if present."""
        load_dotenv(dotenv_path or PROJECT_ROOT / ".env")

        def get(name: str, default: str = "") -> str:
            # .strip("'\"") catches the common mistake of quoting values in
            # .env, which otherwise produces a baffling authentication error.
            return (os.getenv(name) or default).strip().strip("'\"")

        missing: list[str] = []

        openai_api_key = get("OPENAI_API_KEY")
        if not openai_api_key:
            missing.append("OPENAI_API_KEY")

        sheet_id = get("GOOGLE_SHEET_ID")
        if not sheet_id:
            missing.append("GOOGLE_SHEET_ID")

        key_file = get("GOOGLE_SERVICE_ACCOUNT_FILE")
        if not key_file:
            missing.append("GOOGLE_SERVICE_ACCOUNT_FILE")

        bot_token = get("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")

        chat_id = get("TELEGRAM_CHAT_ID")
        if not chat_id:
            missing.append("TELEGRAM_CHAT_ID")

        if missing:
            raise ConfigError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ".\nCopy .env.example to .env and fill them in."
            )

        key_path = Path(key_file)
        if not key_path.is_absolute():
            key_path = PROJECT_ROOT / key_path
        if not key_path.is_file():
            raise ConfigError(
                f"GOOGLE_SERVICE_ACCOUNT_FILE points at {key_path}, which does "
                f"not exist. Download the service-account JSON key and place it "
                f"there."
            )

        return cls(
            openai_api_key=openai_api_key,
            openai_model=get("OPENAI_MODEL") or DEFAULT_MODEL,
            openai_base_url=get("OPENAI_BASE_URL") or None,
            google_service_account_file=key_path,
            google_sheet_id=sheet_id,
            google_worksheet_name=get("GOOGLE_WORKSHEET_NAME") or DEFAULT_WORKSHEET,
            telegram_bot_token=bot_token,
            telegram_chat_id=chat_id,
            log_level=(get("LOG_LEVEL") or DEFAULT_LOG_LEVEL).upper(),
        )
