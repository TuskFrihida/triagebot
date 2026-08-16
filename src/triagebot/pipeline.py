"""Pipeline orchestration.

Wires the five independently tested modules together:

    validate -> dedupe -> classify -> persist -> mark processed -> notify

Failure isolation is the substance of this module. Two rules govern it:

1. An inquiry is marked processed **only after the spreadsheet write
   succeeds**. If classification or persistence fails, the inquiry stays
   unmarked and is retried on the next run rather than being lost.

2. A notification failure is a warning, not an error. By that point the
   inquiry is durably recorded; treating Telegram as fatal would either
   discard the record or produce a duplicate row on retry.

One inquiry failing never aborts the batch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from pydantic import ValidationError

from .classifier import ClassificationError, TriageClassifier
from .config import Config
from .dedupe import DuplicateTracker, compute_submission_id
from .models import Inquiry
from .notifier import NotifierError, TelegramNotifier
from .sheets import SheetsError, SheetsWriter

log = logging.getLogger(__name__)


class Outcome(str, Enum):
    """What happened to a single inquiry."""

    PROCESSED = "processed"
    # Stored and marked, but the operator was not notified.
    NOTIFY_FAILED = "notify_failed"
    DUPLICATE = "duplicate"
    INVALID = "invalid"
    FAILED = "failed"


@dataclass
class Report:
    """Aggregate result of one pipeline run."""

    outcomes: list[tuple[str, Outcome]] = field(default_factory=list)

    def record(self, label: str, outcome: Outcome) -> None:
        self.outcomes.append((label, outcome))

    def count(self, outcome: Outcome) -> int:
        return sum(1 for _, item in self.outcomes if item is outcome)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def stored(self) -> int:
        """Inquiries safely written to the sheet, notified or not."""
        return self.count(Outcome.PROCESSED) + self.count(Outcome.NOTIFY_FAILED)

    @property
    def ok(self) -> bool:
        """True when nothing failed outright.

        Duplicates and invalid submissions are expected outcomes, not errors:
        handling them cleanly is the requirement.
        """
        return self.count(Outcome.FAILED) == 0

    def summary(self) -> str:
        parts = [f"{self.total} record(s)"]
        for outcome in Outcome:
            n = self.count(outcome)
            if n:
                parts.append(f"{n} {outcome.value}")
        return ", ".join(parts)


class Pipeline:
    """Processes inquiries end to end."""

    def __init__(
        self,
        classifier: TriageClassifier,
        writer: SheetsWriter,
        notifier: TelegramNotifier,
        tracker: DuplicateTracker | None = None,
    ) -> None:
        self.classifier = classifier
        self.writer = writer
        self.notifier = notifier
        # Deferred so that constructing a Pipeline does no network I/O; the
        # sheet is read on first use instead.
        self._tracker = tracker

    @classmethod
    def from_config(cls, config: Config) -> Pipeline:
        return cls(
            classifier=TriageClassifier(
                api_key=config.openai_api_key,
                model=config.openai_model,
                base_url=config.openai_base_url,
            ),
            writer=SheetsWriter(
                service_account_file=config.google_service_account_file,
                sheet_id=config.google_sheet_id,
                worksheet_name=config.google_worksheet_name,
            ),
            notifier=TelegramNotifier(
                bot_token=config.telegram_bot_token,
                chat_id=config.telegram_chat_id,
            ),
        )

    @property
    def tracker(self) -> DuplicateTracker:
        if self._tracker is None:
            self._tracker = DuplicateTracker.from_sheet(self.writer)
        return self._tracker

    # -- single record -----------------------------------------------------

    def process_one(self, record: object) -> Outcome:
        """Process one raw record. Never raises."""
        # 1. Validate. Malformed input is an expected condition for a public
        #    web form, so it is reported and skipped, never fatal.
        if not isinstance(record, dict):
            log.warning("Skipping non-object record: %r", record)
            return Outcome.INVALID
        try:
            inquiry = Inquiry(**record)
        except (ValidationError, TypeError) as exc:
            log.warning(
                "Skipping invalid submission from %r: %s",
                record.get("email", "<no email>"),
                self._first_error(exc),
            )
            return Outcome.INVALID

        # 2. Identity, then 3. duplicate check -- before any paid API call, so
        #    a duplicate costs nothing.
        submission_id = compute_submission_id(inquiry)
        try:
            if submission_id in self.tracker:
                log.info("Skipping duplicate %s from %s", submission_id, inquiry.email)
                return Outcome.DUPLICATE
        except SheetsError as exc:
            log.error("Could not read existing submissions: %s", exc)
            return Outcome.FAILED

        # 4. Classify.
        try:
            result = self.classifier.classify(inquiry)
        except ClassificationError as exc:
            log.error("Classification failed for %s: %s", inquiry.email, exc)
            return Outcome.FAILED

        # 5. Persist. Until this succeeds the inquiry stays unmarked, so a
        #    failure here means it is retried next run rather than lost.
        try:
            self.writer.append(inquiry, result, submission_id=submission_id)
        except SheetsError as exc:
            log.error("Could not write %s to the sheet: %s", submission_id, exc)
            return Outcome.FAILED

        # 6. Only now is it safe to consider this inquiry handled.
        self.tracker.remember(submission_id)

        # 7. Notify. The record is already durable, so a Telegram outage must
        #    not discard it or cause a duplicate row on retry.
        try:
            self.notifier.notify(inquiry, result, submission_id)
        except NotifierError as exc:
            log.warning(
                "Stored %s but could not notify Telegram: %s", submission_id, exc
            )
            return Outcome.NOTIFY_FAILED

        return Outcome.PROCESSED

    # -- batch -------------------------------------------------------------

    def run(self, records: list) -> Report:
        """Process every record. One failure never aborts the batch."""
        report = Report()
        for index, record in enumerate(records, start=1):
            label = self._label(record, index)
            try:
                outcome = self.process_one(record)
            except Exception as exc:  # noqa: BLE001 - last line of defence
                # process_one is written not to raise; if it ever does, the
                # remaining inquiries must still be processed.
                log.exception("Unexpected error processing %s: %s", label, exc)
                outcome = Outcome.FAILED
            report.record(label, outcome)

        log.info("Run complete: %s", report.summary())
        return report

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _label(record: object, index: int) -> str:
        if isinstance(record, dict):
            email = str(record.get("email") or "").strip()
            if email:
                return email
        return f"record #{index}"

    @staticmethod
    def _first_error(exc: Exception) -> str:
        if isinstance(exc, ValidationError) and exc.errors():
            first = exc.errors()[0]
            location = ".".join(str(part) for part in first.get("loc", ())) or "input"
            return f"{location}: {first.get('msg', 'invalid')}"
        return str(exc)
