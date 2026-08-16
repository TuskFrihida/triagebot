"""Telegram notifications.

Independently testable: construct a ``TelegramNotifier`` with an explicit token
and chat id, or use ``from_env()``. Nothing here touches OpenAI or Sheets.

The Bot API is a plain HTTPS POST to /sendMessage, which is why this uses
``requests`` rather than a bot framework -- we send one message and exit, we
never poll for updates or maintain a session.

Formatting uses parse_mode=HTML deliberately. MarkdownV2 requires escaping
eighteen characters including "." and "-", so an ordinary customer message
("Our app crashed - please help.") would fail to parse and the notification
would silently never arrive. HTML needs only &, < and >, which html.escape
handles correctly.
"""

from __future__ import annotations

import html
import logging
import os
import time

import requests

from .models import Inquiry, Priority, TriageResult

log = logging.getLogger(__name__)

# Telegram rejects messages longer than this.
MAX_MESSAGE_CHARS = 4096

# Leaves room for the HTML scaffolding around the body.
_SUMMARY_BUDGET = 900

_PRIORITY_ICON = {
    Priority.HIGH: "\U0001f534",    # red circle
    Priority.MEDIUM: "\U0001f7e0",  # orange circle
    Priority.LOW: "\U0001f7e2",     # green circle
}


class NotifierError(RuntimeError):
    """Raised when a notification could not be delivered.

    The pipeline catches this and continues: by the time we notify, the
    inquiry is already persisted, so a Telegram outage must not lose data.
    """


class TelegramNotifier:
    """Sends formatted triage notifications to a Telegram chat."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token must not be empty")
        if not chat_id:
            raise ValueError("chat_id must not be empty")
        self._token = bot_token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.max_retries = max_retries

    @classmethod
    def from_env(cls) -> TelegramNotifier:
        token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip().strip("'\"")
        chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not set")
        if not chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is not set")
        return cls(bot_token=token, chat_id=chat_id)

    # -- public ------------------------------------------------------------

    def notify(
        self, inquiry: Inquiry, result: TriageResult, submission_id: str
    ) -> None:
        """Send a formatted triage notification. Raises NotifierError."""
        self.send_html(self.format_message(inquiry, result, submission_id))
        log.info("Notified Telegram about %s", submission_id)

    def send_html(self, text: str) -> None:
        """Send one HTML message, retrying transient failures."""
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[: MAX_MESSAGE_CHARS - 20] + "\n[truncated]"

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            # Summaries often mention a URL; a link preview card would bury
            # the triage information the reader actually needs.
            "disable_web_page_preview": True,
        }

        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{self._token}/sendMessage",
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                log.warning("Telegram attempt %d failed: %s", attempt + 1, last_error)
                time.sleep(min(2**attempt, 8))
                continue

            try:
                body = response.json()
            except ValueError:
                last_error = f"non-JSON response (HTTP {response.status_code})"
                body = {}

            if body.get("ok"):
                return

            description = body.get("description", last_error)

            # 429 carries the exact wait in parameters.retry_after; honouring it
            # is both faster and politer than a fixed backoff.
            if response.status_code == 429:
                wait = int(body.get("parameters", {}).get("retry_after", 1))
                log.warning("Telegram rate limited; retrying in %ds", wait)
                time.sleep(min(wait, 30))
                last_error = description
                continue

            # 4xx other than 429 will never succeed on retry: a bad token, a
            # chat the bot cannot reach, or malformed HTML.
            if 400 <= response.status_code < 500:
                raise NotifierError(
                    f"Telegram rejected the message ({response.status_code}): "
                    f"{description}"
                )

            last_error = description
            log.warning("Telegram attempt %d failed: %s", attempt + 1, description)
            time.sleep(min(2**attempt, 8))

        raise NotifierError(f"Telegram send failed after retries: {last_error}")

    # -- formatting --------------------------------------------------------

    @staticmethod
    def format_message(
        inquiry: Inquiry, result: TriageResult, submission_id: str
    ) -> str:
        """Build the HTML message body.

        Every interpolated value is escaped: all of it originates from an
        untrusted web form, and an unescaped "<" would either break parsing or
        let a submitter inject markup into the client's notifications.
        """
        esc = html.escape
        icon = _PRIORITY_ICON.get(result.priority, "")

        summary = result.summary
        if len(summary) > _SUMMARY_BUDGET:
            summary = summary[:_SUMMARY_BUDGET] + "..."

        return (
            f"{icon} <b>{esc(result.priority.value)} priority</b> "
            f"&middot; {esc(result.category.value)}\n"
            f"\n"
            f"<b>{esc(inquiry.name)}</b>\n"
            f"<code>{esc(inquiry.email)}</code>\n"
            f"\n"
            f"{esc(summary)}\n"
            f"\n"
            f"<i>ref {esc(submission_id)}</i>"
        )
