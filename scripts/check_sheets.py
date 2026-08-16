"""Verify the Google Sheets module end to end.

Writes a real row to the real spreadsheet, reads it back, and removes it
again unless --keep is passed. This is the only way to prove *write* access:
opening a spreadsheet succeeds with Viewer permission, so a successful open
says nothing about whether append will work.

Also proves the formula-injection defence: a message beginning with "=" must
come back as literal text, not as an evaluated formula.

    python scripts/check_sheets.py           # write, verify, clean up
    python scripts/check_sheets.py --keep    # leave the test row in place

Cost: zero. The Google Sheets API is free at this volume.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from triagebot.models import Category, Inquiry, Priority, TriageResult
from triagebot.sheets import HEADERS, SheetsError, SheetsWriter

PROJECT_ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
for noisy in ("urllib3", "google", "google_auth_httplib2", "googleapiclient"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("check_sheets")

# A live formula if the sheet ever evaluates it, harmless text if it does not.
INJECTION_PROBE = '=1+1 and =IMPORTXML("https://example.com","//x")'

TEST_ID = "TEST-CHECK-SHEETS"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="do not delete the test row")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    try:
        writer = SheetsWriter.from_env()
    except SheetsError as exc:
        log.error("%s", exc)
        return 1

    log.info("[1/4] Connect and ensure worksheet exists")
    try:
        worksheet = writer.worksheet
    except SheetsError as exc:
        log.error("  %s", exc)
        return 1
    log.info("  OK -- worksheet %r, %d rows", worksheet.title, worksheet.row_count)

    header_row = worksheet.row_values(1)
    if header_row != HEADERS:
        log.error("  header mismatch\n    got:      %s\n    expected: %s",
                  header_row, HEADERS)
        return 1
    log.info("  OK -- header row matches (%d columns)", len(HEADERS))

    log.info("[2/4] Append a row (proves WRITE access, not just read)")
    inquiry = Inquiry(
        name="Check Script",
        email="check@example.com",
        message=INJECTION_PROBE,
    )
    result = TriageResult(
        summary="Verification row written by scripts/check_sheets.py.",
        category=Category.GENERAL_QUESTION,
        priority=Priority.LOW,
    )
    try:
        writer.append(inquiry, result, submission_id=TEST_ID)
    except SheetsError as exc:
        log.error("  %s", exc)
        return 1
    log.info("  OK -- row appended")

    log.info("[3/4] Read back and check formula injection is inert")
    rows = worksheet.get_all_values()
    written = next((r for r in rows if len(r) > 1 and r[1] == TEST_ID), None)
    if written is None:
        log.error("  appended row could not be found on read-back")
        return 1

    stored_message = written[4]
    if stored_message != INJECTION_PROBE:
        log.error("  message was altered by the sheet!")
        log.error("    sent:   %r", INJECTION_PROBE)
        log.error("    stored: %r", stored_message)
        log.error("  -> the formula was evaluated; value_input_option is wrong")
        return 1
    log.info("  OK -- stored literally, formula NOT evaluated")
    log.info("  row: %s", " | ".join(written[:4]))

    log.info("[4/4] Read back submission IDs (used by dedupe in Step 6)")
    try:
        ids = writer.existing_submission_ids()
    except SheetsError as exc:
        log.error("  %s", exc)
        return 1
    if TEST_ID not in ids:
        log.error("  %s missing from submission ids: %s", TEST_ID, sorted(ids))
        return 1
    log.info("  OK -- %d id(s) present, including the one just written", len(ids))

    if args.keep:
        log.info("Leaving test row in place (--keep).")
    else:
        index = rows.index(written) + 1  # get_all_values is 0-based, sheets are 1-based
        worksheet.delete_rows(index)
        log.info("Test row removed; sheet left clean.")

    log.info("-" * 52)
    log.info("Google Sheets: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
