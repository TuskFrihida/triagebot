"""Command-line entrypoint.

    python -m triagebot.cli --input data/inquiries.json

Exit codes are meaningful so this can be scheduled and monitored:

    0  every record reached a defined outcome (processed, duplicate, invalid)
    1  at least one record failed outright, or the run could not start

Note that invalid submissions do not fail the run. Handling them cleanly is a
requirement, not an error condition.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Config, ConfigError
from .logging_setup import configure_logging
from .pipeline import Outcome, Pipeline
from .sources import SourceError, from_json_file

log = logging.getLogger("triagebot.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triagebot",
        description="Triage customer inquiries: summarise, classify, record, notify.",
    )
    parser.add_argument(
        "--input", "-i", required=True, type=Path,
        help="JSON file containing an inquiry object or an array of them",
    )
    parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="override LOG_LEVEL from the environment",
    )
    parser.add_argument(
        "--log-file", type=Path, default=None,
        help="also write logs to this file, for unattended runs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Configure logging before anything else so that even configuration
    # failures are reported through the same channel.
    configure_logging(level=args.log_level or "INFO", log_file=args.log_file)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 1

    # Re-apply now that the configured level is known, unless overridden.
    if args.log_level is None:
        configure_logging(level=config.log_level, log_file=args.log_file)

    log.info(
        "Starting run (model=%s, sheet tab=%r)",
        config.openai_model, config.google_worksheet_name,
    )

    try:
        records = from_json_file(args.input)
    except SourceError as exc:
        log.error("%s", exc)
        return 1

    pipeline = Pipeline.from_config(config)
    report = pipeline.run(records)

    for label, outcome in report.outcomes:
        log.info("  %-34s %s", label, outcome.value)

    log.info(
        "Stored %d of %d record(s); %d duplicate, %d invalid, %d failed",
        report.stored, report.total,
        report.count(Outcome.DUPLICATE),
        report.count(Outcome.INVALID),
        report.count(Outcome.FAILED),
    )
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
