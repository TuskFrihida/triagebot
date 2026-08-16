"""Verify duplicate suppression.

Offline checks cover id stability and the boundaries of what counts as a
duplicate. The live check proves the sheet really is a usable source of
truth: write a row, rebuild the tracker from the spreadsheet, and confirm
the submission is then recognised as already processed.

    python scripts/check_dedupe.py            # offline + one sheet round trip
    python scripts/check_dedupe.py --offline  # no network at all

Cost: zero. No OpenAI calls are made -- deduplication runs before
classification precisely so a duplicate costs nothing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from triagebot.dedupe import DuplicateTracker, compute_submission_id
from triagebot.models import Category, Inquiry, Priority, TriageResult
from triagebot.sheets import SheetsError, SheetsWriter

PROJECT_ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
for noisy in ("urllib3", "google"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("check_dedupe")


BASE = Inquiry(
    name="Ana Reyes",
    email="ana@example.com",
    message="My invoice looks wrong, can you check it?",
)


def check_identity() -> bool:
    """Same content must always produce the same id, across runs."""
    log.info("[1/3] Submission id stability (offline)")

    first = compute_submission_id(BASE)
    if compute_submission_id(BASE) != first:
        log.error("  id is not deterministic")
        return False

    same_cases = {
        "different name": Inquiry(
            name="A. Reyes", email=BASE.email, message=BASE.message),
        "uppercase email": Inquiry(
            name=BASE.name, email="ANA@Example.com", message=BASE.message),
        "extra whitespace": Inquiry(
            name=BASE.name, email=BASE.email,
            message="My invoice looks   wrong,\n can you check it?"),
    }
    for label, variant in same_cases.items():
        if compute_submission_id(variant) != first:
            log.error("  %s produced a different id but should match", label)
            return False

    log.info("  OK -- stable across name, email case and whitespace differences")
    return True


def check_boundaries() -> bool:
    """Genuinely different submissions must NOT collide."""
    log.info("[2/3] Duplicate boundaries (offline)")

    first = compute_submission_id(BASE)
    different_cases = {
        "different message, same person": Inquiry(
            name=BASE.name, email=BASE.email,
            message="Actually, I also need a refund."),
        "same message, different person": Inquiry(
            name="Bob", email="bob@example.com", message=BASE.message),
    }
    for label, variant in different_cases.items():
        if compute_submission_id(variant) == first:
            log.error("  %s collided but should be treated as new", label)
            return False

    tracker = DuplicateTracker([first])
    if not tracker.is_duplicate(BASE):
        log.error("  known submission not recognised as duplicate")
        return False

    second_question = different_cases["different message, same person"]
    if tracker.is_duplicate(second_question):
        log.error("  a customer's SECOND question was suppressed -- this is the "
                  "failure mode that loses the client a customer")
        return False

    # A duplicate later in the same batch must be caught without re-reading.
    fresh = DuplicateTracker()
    new_id = compute_submission_id(BASE)
    if fresh.is_duplicate(BASE):
        log.error("  empty tracker reported a duplicate")
        return False
    fresh.remember(new_id)
    if not fresh.is_duplicate(BASE):
        log.error("  remember() did not take effect within the run")
        return False

    log.info("  OK -- second questions pass through, repeats are caught")
    return True


def check_sheet_round_trip() -> bool:
    """The spreadsheet must work as the cross-restart source of truth."""
    log.info("[3/3] Sheet round trip (live)")
    try:
        writer = SheetsWriter.from_env()
    except SheetsError as exc:
        log.error("  %s", exc)
        return False

    probe = Inquiry(
        name="Dedupe Probe",
        email="dedupe-probe@example.com",
        message="Round-trip verification for scripts/check_dedupe.py",
    )
    probe_id = compute_submission_id(probe)

    try:
        before = DuplicateTracker.from_sheet(writer)
        if probe_id in before:
            log.error("  probe id already present; a previous run left data behind")
            return False

        writer.append(
            probe,
            TriageResult(summary="Dedupe round-trip probe.",
                         category=Category.GENERAL_QUESTION,
                         priority=Priority.LOW),
            submission_id=probe_id,
        )

        # Rebuilt from scratch: this is what a restarted process would see.
        after = DuplicateTracker.from_sheet(writer)
        if not after.is_duplicate(probe):
            log.error("  probe not recognised after re-reading the sheet")
            return False
        log.info("  OK -- id survives a full reload; dedupe works across restarts")
    finally:
        # Always clean up, even if an assertion above failed.
        try:
            worksheet = writer.worksheet
            rows = worksheet.get_all_values()
            for index, row in enumerate(rows, start=1):
                if len(row) > 1 and row[1] == probe_id:
                    worksheet.delete_rows(index)
                    log.info("  probe row removed; sheet left clean")
                    break
        except SheetsError as exc:
            log.warning("  could not clean up probe row: %s", exc)

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="skip the sheet check")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    results = {"identity": check_identity(), "boundaries": check_boundaries()}
    if args.offline:
        log.info("[3/3] Sheet round trip -- SKIPPED (--offline)")
    else:
        results["round trip"] = check_sheet_round_trip()

    log.info("-" * 52)
    for name, ok in results.items():
        log.info("%-12s %s", name, "PASS" if ok else "FAIL")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
