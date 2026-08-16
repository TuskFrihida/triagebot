"""Find your Telegram chat_id.

A chat_id is never shown in the Telegram UI. The only way to learn it is to
send your bot a message and then ask the API what it received.

Prerequisites:
  1. TELEGRAM_BOT_TOKEN is filled in .env
  2. You have sent your bot at least one message (click Start, say "hello")

    python scripts/get_chat_id.py

Cost: zero. The Telegram Bot API is free.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
# A Telegram request URL contains the bot token, so never let the HTTP layer
# log at INFO -- that would write the token to disk in plaintext.
for noisy in ("urllib3", "requests"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("get_chat_id")


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip().strip("'\"")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is not set in .env")
        return 1

    base = f"https://api.telegram.org/bot{token}"

    try:
        me = requests.get(f"{base}/getMe", timeout=10).json()
    except requests.RequestException as exc:
        log.error("Could not reach Telegram: %s", exc)
        return 1

    if not me.get("ok"):
        log.error("Token rejected by Telegram: %s", me.get("description"))
        log.error("-> Re-copy the token from @BotFather; it looks like 123456:AAH...")
        return 1

    log.info("Bot is @%s", me["result"]["username"])

    try:
        payload = requests.get(f"{base}/getUpdates", timeout=15).json()
    except requests.RequestException as exc:
        log.error("Could not reach Telegram: %s", exc)
        return 1

    if not payload.get("ok"):
        log.error("getUpdates failed: %s", payload.get("description"))
        # 409 means a webhook is registered; polling and webhooks are exclusive.
        if payload.get("error_code") == 409:
            log.error("-> A webhook is set. Clear it with: %s/deleteWebhook", "<base>")
        return 1

    # An update may be a message, edited_message, channel_post, etc. Pull the
    # chat off whichever one is present rather than assuming "message".
    seen: dict[int, str] = {}
    for update in payload.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if chat:
                label = chat.get("username") or chat.get("title") or chat.get("first_name") or "?"
                seen[chat["id"]] = f"{chat.get('type', 'unknown')}, {label}"

    if not seen:
        log.error("No messages found.")
        log.error("-> Open your bot in Telegram, press Start, send it any text,")
        log.error("   then run this again. Telegram also drops updates after 24h.")
        return 1

    log.info("-" * 52)
    for chat_id, description in seen.items():
        log.info("TELEGRAM_CHAT_ID=%s   (%s)", chat_id, description)
    log.info("-" * 52)
    log.info("Copy the line above into .env (the 'private' one is you).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
