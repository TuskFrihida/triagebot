"""Pipeline orchestration and failure isolation.

Entirely offline: OpenAI, Sheets and Telegram are replaced with fakes, so the
whole suite runs with no credentials and no spend.

These tests exist to protect four invariants:

1. an inquiry is marked processed only after the spreadsheet write succeeds;
2. a notification failure never loses the inquiry and never causes it to be
   reprocessed into a duplicate row;
3. one inquiry failing never aborts the batch;
4. duplicates and invalid submissions are expected outcomes, not errors.
"""

from __future__ import annotations

import pytest

from triagebot.classifier import ClassificationError
from triagebot.dedupe import DuplicateTracker, compute_submission_id
from triagebot.models import Category, Inquiry, Priority, TriageResult
from triagebot.notifier import NotifierError
from triagebot.pipeline import Outcome, Pipeline
from triagebot.sheets import SheetsError

VALID = {
    "name": "Dana Whitfield",
    "email": "dana@northwind.example",
    "message": "We are locked out and cannot access any dashboards.",
}

RESULT = TriageResult(
    summary="The team is locked out and cannot access dashboards.",
    category=Category.TECHNICAL_SUPPORT,
    priority=Priority.HIGH,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeClassifier:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[Inquiry] = []

    def classify(self, inquiry: Inquiry) -> TriageResult:
        self.calls.append(inquiry)
        if self.error:
            raise self.error
        return RESULT


class FakeWriter:
    def __init__(self, error: Exception | None = None, existing: set | None = None):
        self.error = error
        self.rows: list[tuple] = []
        self.existing = existing or set()

    def append(self, inquiry, result, submission_id, timestamp=None) -> None:
        if self.error:
            raise self.error
        self.rows.append((inquiry, result, submission_id))

    def existing_submission_ids(self) -> set:
        return self.existing


class FakeNotifier:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.sent: list[str] = []

    def notify(self, inquiry, result, submission_id) -> None:
        if self.error:
            raise self.error
        self.sent.append(submission_id)


def build(classifier=None, writer=None, notifier=None, known=()):
    return Pipeline(
        classifier=classifier or FakeClassifier(),
        writer=writer or FakeWriter(),
        notifier=notifier or FakeNotifier(),
        tracker=DuplicateTracker(known),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_processes_a_valid_inquiry_end_to_end(self):
        classifier, writer, notifier = FakeClassifier(), FakeWriter(), FakeNotifier()
        pipeline = build(classifier, writer, notifier)

        assert pipeline.process_one(VALID) is Outcome.PROCESSED
        assert len(classifier.calls) == 1
        assert len(writer.rows) == 1
        assert len(notifier.sent) == 1

    def test_records_the_original_inquiry_and_the_ai_result(self):
        writer = FakeWriter()
        build(writer=writer).process_one(VALID)

        inquiry, result, submission_id = writer.rows[0]
        assert inquiry.email == VALID["email"]
        assert result.category is Category.TECHNICAL_SUPPORT
        assert submission_id == compute_submission_id(Inquiry(**VALID))


# ---------------------------------------------------------------------------
# Expected non-error outcomes
# ---------------------------------------------------------------------------

class TestDuplicates:
    def test_known_submission_is_skipped(self):
        known = {compute_submission_id(Inquiry(**VALID))}
        classifier, writer = FakeClassifier(), FakeWriter()
        pipeline = build(classifier, writer, known=known)

        assert pipeline.process_one(VALID) is Outcome.DUPLICATE
        assert classifier.calls == [], "a duplicate must cost no API call"
        assert writer.rows == []

    def test_repeat_within_the_same_batch_is_caught(self):
        classifier = FakeClassifier()
        pipeline = build(classifier)

        report = pipeline.run([VALID, dict(VALID)])
        assert report.count(Outcome.PROCESSED) == 1
        assert report.count(Outcome.DUPLICATE) == 1
        assert len(classifier.calls) == 1


class TestInvalidInput:
    @pytest.mark.parametrize(
        "record",
        [
            pytest.param({"name": "A", "email": "nope", "message": "hi"},
                         id="bad email"),
            pytest.param({"name": "", "email": "a@b.co", "message": "hi"},
                         id="blank name"),
            pytest.param({"name": "A", "email": "a@b.co", "message": "  "},
                         id="blank message"),
            pytest.param({"name": "A", "email": "a@b.co"}, id="missing field"),
            pytest.param("a bare string", id="not an object"),
            pytest.param(None, id="null"),
        ],
    )
    def test_malformed_records_are_reported_not_raised(self, record):
        classifier = FakeClassifier()
        assert build(classifier).process_one(record) is Outcome.INVALID
        assert classifier.calls == [], "invalid input must cost no API call"


# ---------------------------------------------------------------------------
# Failure isolation -- the invariants
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_classification_failure_leaves_the_inquiry_unmarked(self):
        # Invariant 1: it must be retried next run, not silently dropped.
        writer, notifier = FakeWriter(), FakeNotifier()
        pipeline = build(FakeClassifier(ClassificationError("boom")), writer, notifier)

        assert pipeline.process_one(VALID) is Outcome.FAILED
        assert writer.rows == []
        assert notifier.sent == []
        assert compute_submission_id(Inquiry(**VALID)) not in pipeline.tracker

    def test_sheet_failure_leaves_the_inquiry_unmarked_and_unnotified(self):
        # Invariant 1: notifying about something we failed to record would be
        # worse than staying silent.
        notifier = FakeNotifier()
        pipeline = build(writer=FakeWriter(SheetsError("denied")), notifier=notifier)

        assert pipeline.process_one(VALID) is Outcome.FAILED
        assert notifier.sent == []
        assert compute_submission_id(Inquiry(**VALID)) not in pipeline.tracker

    def test_notification_failure_keeps_the_inquiry_and_marks_it_processed(self):
        # Invariant 2: the row is already durable. Reprocessing it next run
        # would duplicate it in the client's spreadsheet.
        writer = FakeWriter()
        pipeline = build(writer=writer, notifier=FakeNotifier(NotifierError("down")))

        assert pipeline.process_one(VALID) is Outcome.NOTIFY_FAILED
        assert len(writer.rows) == 1
        assert compute_submission_id(Inquiry(**VALID)) in pipeline.tracker

    def test_notification_outage_does_not_duplicate_on_the_next_run(self):
        writer = FakeWriter()
        pipeline = build(writer=writer, notifier=FakeNotifier(NotifierError("down")))

        pipeline.process_one(VALID)
        assert pipeline.process_one(VALID) is Outcome.DUPLICATE
        assert len(writer.rows) == 1

    def test_one_bad_record_does_not_abort_the_batch(self):
        # Invariant 3.
        second = dict(VALID, email="other@example.com", message="A different issue.")
        report = build().run([{"broken": True}, VALID, "junk", second])

        assert report.count(Outcome.PROCESSED) == 2
        assert report.count(Outcome.INVALID) == 2
        assert report.total == 4

    def test_an_unexpected_exception_is_contained(self):
        # Invariant 3, last line of defence: process_one is written not to
        # raise, but the batch must survive it if it ever does.
        class Exploding:
            def classify(self, inquiry):
                raise RuntimeError("something nobody predicted")

        report = build(classifier=Exploding()).run([VALID])
        assert report.count(Outcome.FAILED) == 1


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class TestReport:
    def test_duplicates_and_invalid_records_do_not_fail_the_run(self):
        # Invariant 4: handling them cleanly is the requirement, so the exit
        # code must stay zero and remain usable for scheduling.
        report = build().run([VALID, dict(VALID), {"bad": True}])
        assert report.ok
        assert report.count(Outcome.FAILED) == 0

    def test_a_genuine_failure_fails_the_run(self):
        pipeline = build(FakeClassifier(ClassificationError("boom")))
        assert not pipeline.run([VALID]).ok

    def test_stored_counts_inquiries_that_were_recorded_but_not_notified(self):
        pipeline = build(notifier=FakeNotifier(NotifierError("down")))
        report = pipeline.run([VALID])
        assert report.stored == 1
        assert report.ok, "a notification outage is not a run failure"

    def test_summary_mentions_every_outcome_that_occurred(self):
        summary = build().run([VALID, dict(VALID), {"bad": True}]).summary()
        assert "3 record(s)" in summary
        assert "1 processed" in summary
        assert "1 duplicate" in summary
        assert "1 invalid" in summary
