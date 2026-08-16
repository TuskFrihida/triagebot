"""Verify the Telegram notifier.

Offline checks cover HTML escaping and truncation. The live check sends real
messages to the configured chat -- look at your phone to confirm they render
correctly, which is the only way to verify formatting.

    python scripts/check_telegram.py             # offline + 3 real messages
    python scripts/check_telegram.py --offline   # no messages sent

Cost: zero. The Telegram Bot API is free.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from triagebot.models import Category, Inquiry, Priority, TriageResult
from triagebot.notifier import MAX_MESSAGE_CHARS, NotifierError, TelegramNotifier

PROJECT_ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
for noisy in ("urllib3", "requests"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("check_telegram")


# Every one of these characters is either HTML-significant or a MarkdownV2
# special character. A real customer message can contain all of them.
HOSTILE = Inquiry(
    name="<script>alert('xss')</script> O'Brien & Sons",
    email="a+b@example.com",
    message="Our app crashed - please help. Cost is 5 < 10 & >20% failed!",
)


def check_escaping() -> bool:
    log.info("[1/3] HTML escaping (offline)")

    result = TriageResult(
        summary="Customer reports a crash; costs 5 < 10 & >20% of runs failed.",
        category=Category.TECHNICAL_SUPPORT,
        priority=Priority.HIGH,
    )
    body = TelegramNotifier.format_message(HOSTILE, result, "ESC-TEST-1")

    # The raw dangerous sequences must not survive into the payload.
    for forbidden in ("<script>", "</script>", "alert('xss')"):
        if forbidden in body:
            log.error("  unescaped %r present in message body", forbidden)
            return False

    # ...and must appear in escaped form instead.
    for expected in ("&lt;script&gt;", "&amp;"):
        if expected not in body:
            log.error("  expected escaped sequence %r not found", expected)
            return False

    # Only our own tags should remain as real markup.
    allowed_tags = body.count("<b>") + body.count("</b>") + body.count("<code>") \
        + body.count("</code>") + body.count("<i>") + body.count("</i>")
    if body.count("<") != allowed_tags:
        log.error("  unexpected raw '<' in body:\n%s", body)
        return False

    log.info("  OK -- script tags neutralised, only our own markup remains")
    return True


def check_truncation() -> bool:
    log.info("[2/3] Length limits (offline)")

    result = TriageResult(
        summary="x" * 5000,
        category=Category.GENERAL_QUESTION,
        priority=Priority.LOW,
    )
    body = TelegramNotifier.format_message(
        Inquiry(name="Long", email="l@example.com", message="hi"),
        result,
        "LEN-TEST-1",
    )
    if len(body) >= MAX_MESSAGE_CHARS:
        log.error("  formatted body is %d chars, over the %d limit",
                  len(body), MAX_MESSAGE_CHARS)
        return False

    log.info("  OK -- 5000-char summary reduced to a %d-char message", len(body))
    return True


def check_live() -> bool:
    log.info("[3/3] Live send (3 real messages)")
    try:
        notifier = TelegramNotifier.from_env()
    except ValueError as exc:
        log.error("  %s", exc)
        return False

    cases = [
        (
            Inquiry(name="Dana Whitfield", email="dana@northwind.example",
                    message="Locked out since the update."),
            TriageResult(
                summary="The entire team is locked out with a 500 error after "
                        "this morning's update; roughly 40 users are blocked.",
                category=Category.TECHNICAL_SUPPORT, priority=Priority.HIGH),
            "DEMO-HIGH-1",
        ),
        (
            Inquiry(name="Marcus Bell", email="m.bell@example.org",
                    message="Evaluating Enterprise."),
            TriageResult(
                summary="A 200-seat prospect is requesting Enterprise pricing "
                        "and a demo next week.",
                category=Category.SALES, priority=Priority.MEDIUM),
            "DEMO-MED-1",
        ),
        # Proves escaping survives a real round trip, not just a local assert.
        (
            HOSTILE,
            TriageResult(
                summary="Escaping check: 5 < 10 & >20% failed - see O'Brien.",
                category=Category.GENERAL_QUESTION, priority=Priority.LOW),
            "DEMO-ESCAPE-1",
        ),
    ]

    for inquiry, result, ref in cases:
        try:
            notifier.notify(inquiry, result, ref)
        except NotifierError as exc:
            log.error("  FAILED to send %s: %s", ref, exc)
            return False

    log.info("  OK -- 3 messages delivered; check your phone that they render")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="skip live sends")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    results = {"escaping": check_escaping(), "truncation": check_truncation()}
    if args.offline:
        log.info("[3/3] Live send -- SKIPPED (--offline)")
    else:
        results["live"] = check_live()

    log.info("-" * 52)
    for name, ok in results.items():
        log.info("%-12s %s", name, "PASS" if ok else "FAIL")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
