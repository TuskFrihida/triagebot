"""Verify the classifier.

By default runs ONLY offline checks and costs nothing:
  * the JSON Schema derived from TriageResult satisfies strict-mode rules
  * Inquiry validation accepts good input and rejects bad input

    python scripts/check_classifier.py            # free, no API calls
    python scripts/check_classifier.py --live     # ONE real API call
    python scripts/check_classifier.py --live-all # FIVE real API calls

At gpt-5-nano rates a live run costs roughly $0.00004 per inquiry.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from triagebot.classifier import ClassificationError, TriageClassifier
from triagebot.models import Category, Inquiry, TriageResult

PROJECT_ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "urllib3", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("check_classifier")


# --------------------------------------------------------------------------
# Offline checks
# --------------------------------------------------------------------------

def check_schema() -> bool:
    """Assert the schema meets OpenAI strict-mode requirements."""
    log.info("[1/3] Schema (offline)")

    # Prefer the SDK's own transform so we inspect what is actually sent to
    # the API, not merely Pydantic's default output. Private API, hence the
    # fallback -- this is a diagnostic, not production code.
    try:
        from openai.lib._pydantic import to_strict_json_schema

        schema = to_strict_json_schema(TriageResult)
        source = "openai SDK strict transform"
    except Exception:  # noqa: BLE001
        schema = TriageResult.model_json_schema()
        source = "pydantic (SDK transform unavailable)"

    log.info("  schema source: %s", source)

    ok = True
    if schema.get("additionalProperties") is not False:
        log.error("  additionalProperties is not false")
        ok = False

    required = set(schema.get("required", []))
    expected = {"summary", "category", "priority"}
    if required != expected:
        log.error("  required=%s, expected %s", sorted(required), sorted(expected))
        ok = False

    # The enum is the whole point: it makes an out-of-taxonomy category
    # impossible to generate rather than merely unlikely.
    blob = json.dumps(schema)
    for member in Category:
        if member.value not in blob:
            log.error("  category %r missing from schema", member.value)
            ok = False

    if ok:
        log.info("  OK -- strict, 3 required fields, 4 categories enumerated")
    return ok


def check_validation() -> bool:
    """Invalid submissions must be rejected cleanly, never crash."""
    log.info("[2/3] Input validation (offline)")

    good = Inquiry(name="  Ana Reyes ", email="ana@example.com", message=" Help! ")
    if good.name != "Ana Reyes" or good.message != "Help!":
        log.error("  whitespace was not stripped: %r", good)
        return False

    bad_cases = {
        "blank name": {"name": "   ", "email": "a@b.co", "message": "hi"},
        "blank message": {"name": "Ana", "email": "a@b.co", "message": ""},
        "no @ in email": {"name": "Ana", "email": "not-an-email", "message": "hi"},
        "no TLD in email": {"name": "Ana", "email": "ana@localhost", "message": "hi"},
        "missing field": {"name": "Ana", "email": "a@b.co"},
    }
    for label, payload in bad_cases.items():
        try:
            Inquiry(**payload)
        except ValidationError:
            continue
        log.error("  %s was accepted but should have been rejected", label)
        return False

    # Oversized input is truncated rather than rejected, to bound token spend.
    huge = Inquiry(name="Ana", email="a@b.co", message="x" * 10_000)
    if not huge.message.endswith("[truncated]") or len(huge.message) > 4100:
        log.error("  oversized message not truncated (len=%d)", len(huge.message))
        return False

    log.info("  OK -- 1 valid accepted, 5 invalid rejected, oversized truncated")
    return True


# --------------------------------------------------------------------------
# Live checks
# --------------------------------------------------------------------------

LIVE_CASES: list[tuple[Inquiry, Category]] = [
    (
        Inquiry(
            name="Dana Whitfield",
            email="dana@northwind.example",
            message=(
                "Our whole team is locked out since this morning's update. "
                "Login returns error 500 and we cannot access any dashboards. "
                "This is blocking about 40 people."
            ),
        ),
        Category.TECHNICAL_SUPPORT,
    ),
    (
        Inquiry(
            name="Marcus Bell",
            email="m.bell@example.org",
            message=(
                "We are a 200-seat company evaluating your Enterprise tier. "
                "Could you send pricing and arrange a demo next week?"
            ),
        ),
        Category.SALES,
    ),
    (
        Inquiry(
            name="Priya Raman",
            email="priya@example.net",
            message=(
                "I was charged twice for March, invoice 8841 and 8842, "
                "same amount. Please refund the duplicate."
            ),
        ),
        Category.BILLING,
    ),
    (
        Inquiry(
            name="Tom Okafor",
            email="tom@example.com",
            message="Do you have any internships for students this summer?",
        ),
        Category.GENERAL_QUESTION,
    ),
    (
        Inquiry(
            name="Lena Fischer",
            email="lena@example.de",
            message="hi",  # near-empty but valid: must not crash
        ),
        None,  # no expectation; we only require that it does not blow up
    ),
]


def check_live(limit: int) -> bool:
    log.info("[3/3] Live API (%d call%s)", limit, "" if limit == 1 else "s")
    try:
        classifier = TriageClassifier.from_env()
    except ValueError as exc:
        log.error("  %s", exc)
        return False

    log.info("  model:   %s", classifier.model)
    log.info("  backend: %s", classifier.backend)
    ok = True
    for inquiry, expected in LIVE_CASES[:limit]:
        try:
            result = classifier.classify(inquiry)
        except ClassificationError as exc:
            log.error("  FAILED for %s: %s", inquiry.email, exc)
            ok = False
            continue

        # The enum guarantees membership; assert it anyway so a future schema
        # regression is caught loudly.
        assert isinstance(result.category, Category)

        verdict = ""
        if expected is not None:
            match = result.category is expected
            verdict = "  <-- MISMATCH, expected %s" % expected.value if not match else ""
            ok = ok and match

        log.info("  %-28s %-18s %-6s %s%s", inquiry.name, result.category.value,
                 result.priority.value, result.summary, verdict)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="one real API call")
    parser.add_argument("--live-all", action="store_true", help="five real API calls")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    results = {"schema": check_schema(), "validation": check_validation()}
    if args.live or args.live_all:
        results["live"] = check_live(len(LIVE_CASES) if args.live_all else 1)
    else:
        log.info("[3/3] Live API -- SKIPPED (pass --live to enable)")

    log.info("-" * 60)
    for name, ok in results.items():
        log.info("%-12s %s", name, "PASS" if ok else "FAIL")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
