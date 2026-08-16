"""Verify that all three sets of credentials actually work.

Run this after filling in .env, BEFORE writing any integration code. It is
deliberately self-contained -- it imports nothing from triagebot -- so that a
credential problem can never be confused with a bug in our own modules.

Cost: zero. The OpenAI check lists models (no tokens billed); the Google and
Telegram calls are free.

    python scripts/check_credentials.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(message)s",
)
# The OpenAI SDK and gspread log every HTTP request at INFO via httpx/urllib3.
# Useful when debugging transport, noise otherwise -- and a request URL can
# carry a bot token, so keep these quiet by default.
# NOTE: openai 3.x vendors its transport as "httpx2"/"httpcore2", not "httpx".
# Names below were confirmed by inspecting logging.root.manager.loggerDict,
# not assumed.
for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "urllib3", "google", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("check_credentials")


def _require(name: str) -> str | None:
    """Return env var `name`, or None (with a log line) if unset/blank."""
    value = (os.getenv(name) or "").strip()
    if not value:
        log.error("  %s is not set in .env", name)
        return None
    return value


def check_openai() -> bool:
    log.info("[1/3] OpenAI")
    api_key = _require("OPENAI_API_KEY")
    if not api_key:
        return False

    # Catch the classic copy-paste mistakes before spending a round trip.
    if api_key.startswith(("'", '"')) or api_key.endswith(("'", '"')):
        log.error("  Key has surrounding quotes -- remove them from .env")
        return False

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        models = client.models.list()
        count = len(list(models.data))
    except Exception as exc:  # noqa: BLE001 - report any failure to the operator
        log.error("  FAILED: %s: %s", type(exc).__name__, exc)
        return False

    log.info("  OK -- key is valid, %d models visible to this account", count)
    return True


def check_google_sheets() -> bool:
    log.info("[2/3] Google Sheets")
    key_path_raw = _require("GOOGLE_SERVICE_ACCOUNT_FILE")
    sheet_id = _require("GOOGLE_SHEET_ID")
    if not key_path_raw or not sheet_id:
        return False

    key_path = Path(key_path_raw)
    if not key_path.is_absolute():
        key_path = PROJECT_ROOT / key_path
    if not key_path.is_file():
        log.error("  Key file not found at %s", key_path)
        return False

    # Surface the robot's address up front: if the next step 403s, this is the
    # address that needs to be shared on the sheet.
    try:
        client_email = json.loads(key_path.read_text(encoding="utf-8"))["client_email"]
    except (ValueError, KeyError, OSError) as exc:
        log.error("  Key file is not a valid service-account JSON: %s", exc)
        return False
    log.info("  service account: %s", client_email)

    try:
        import gspread

        gc = gspread.service_account(filename=str(key_path))
        sheet = gc.open_by_key(sheet_id)
        tabs = [ws.title for ws in sheet.worksheets()]
    except Exception as exc:  # noqa: BLE001
        log.error("  FAILED: %s: %s", type(exc).__name__, exc)
        if "PERMISSION_DENIED" in str(exc) or "403" in str(exc):
            log.error("  -> Share the sheet with %s as Editor", client_email)
        elif "404" in str(exc):
            log.error("  -> GOOGLE_SHEET_ID looks wrong; copy it from the sheet URL")
        return False

    log.info("  OK -- opened '%s', tabs: %s", sheet.title, ", ".join(tabs))

    wanted = (os.getenv("GOOGLE_WORKSHEET_NAME") or "Inquiries").strip()
    if wanted not in tabs:
        log.info("  note: tab '%s' does not exist yet; Step 4 will create it", wanted)
    return True


def check_telegram() -> bool:
    log.info("[3/3] Telegram")
    token = _require("TELEGRAM_BOT_TOKEN")
    chat_id = _require("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    import requests

    base = f"https://api.telegram.org/bot{token}"
    try:
        me = requests.get(f"{base}/getMe", timeout=10).json()
    except requests.RequestException as exc:
        log.error("  FAILED: could not reach Telegram: %s", exc)
        return False

    if not me.get("ok"):
        log.error("  Token rejected: %s", me.get("description"))
        return False
    log.info("  OK -- bot is @%s", me["result"]["username"])

    try:
        sent = requests.post(
            f"{base}/sendMessage",
            json={"chat_id": chat_id, "text": "TriageBot: credential check OK."},
            timeout=10,
        ).json()
    except requests.RequestException as exc:
        log.error("  FAILED: could not reach Telegram: %s", exc)
        return False

    if not sent.get("ok"):
        log.error("  Could not send to chat %s: %s", chat_id, sent.get("description"))
        log.error("  -> Send your bot a message first, then re-check TELEGRAM_CHAT_ID")
        return False

    log.info("  OK -- test message delivered to chat %s", chat_id)
    return True


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")

    results = {
        "OpenAI": check_openai(),
        "Google Sheets": check_google_sheets(),
        "Telegram": check_telegram(),
    }

    log.info("-" * 52)
    for name, ok in results.items():
        log.info("%-16s %s", name, "PASS" if ok else "FAIL")

    if all(results.values()):
        log.info("All credentials verified.")
        return 0
    log.error("Fix the FAIL items above before continuing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
