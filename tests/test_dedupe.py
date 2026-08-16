"""Duplicate suppression.

The most important test here is the negative one: a customer's second,
different question must NOT be suppressed. That is the failure mode that
would cost the client a customer, and it is what a naive email-based
deduplication gets wrong.
"""

from __future__ import annotations

import pytest

from triagebot.dedupe import DuplicateTracker, compute_submission_id
from triagebot.models import Inquiry

BASE = Inquiry(
    name="Ana Reyes",
    email="ana@example.com",
    message="My invoice looks wrong, can you check it?",
)


def variant(**overrides) -> Inquiry:
    payload = {"name": BASE.name, "email": BASE.email, "message": BASE.message}
    payload.update(overrides)
    return Inquiry(**payload)


class TestSubmissionId:
    def test_is_deterministic(self):
        assert compute_submission_id(BASE) == compute_submission_id(BASE)

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"name": "A. Reyes"}, id="different name"),
            pytest.param({"email": "ANA@Example.com"}, id="email case"),
            pytest.param({"email": "  ana@example.com  "}, id="email whitespace"),
            pytest.param(
                {"message": "My invoice looks   wrong,\n can you check it?"},
                id="collapsed whitespace",
            ),
        ],
    )
    def test_ignores_cosmetic_differences(self, overrides):
        assert compute_submission_id(variant(**overrides)) == compute_submission_id(BASE)

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"message": "Actually, I also need a refund."},
                         id="different message"),
            pytest.param({"email": "bob@example.com"}, id="different person"),
        ],
    )
    def test_distinguishes_genuinely_different_submissions(self, overrides):
        assert compute_submission_id(variant(**overrides)) != compute_submission_id(BASE)

    def test_fields_cannot_bleed_into_one_another(self):
        # Without a separator, ("ab", "c") and ("a", "bc") would collide.
        left = Inquiry(name="X", email="ab@example.com", message="c")
        right = Inquiry(name="X", email="a@example.com", message="bc")
        assert compute_submission_id(left) != compute_submission_id(right)


class TestDuplicateTracker:
    def test_recognises_a_known_submission(self):
        tracker = DuplicateTracker([compute_submission_id(BASE)])
        assert tracker.is_duplicate(BASE)

    def test_does_not_suppress_a_second_different_question(self):
        # If this ever fails, the client loses a customer.
        tracker = DuplicateTracker([compute_submission_id(BASE)])
        follow_up = variant(message="Actually, I also need a refund.")
        assert not tracker.is_duplicate(follow_up)

    def test_empty_tracker_suppresses_nothing(self):
        assert not DuplicateTracker().is_duplicate(BASE)

    def test_remember_catches_repeats_later_in_the_same_batch(self):
        tracker = DuplicateTracker()
        tracker.remember(compute_submission_id(BASE))
        assert tracker.is_duplicate(BASE)

    def test_ignores_blank_ids_from_the_sheet(self):
        # Trailing empty cells are normal in a spreadsheet column.
        assert len(DuplicateTracker(["abc", "", None])) == 1

    def test_from_sheet_reads_the_source_of_truth(self):
        class FakeWriter:
            def existing_submission_ids(self):
                return {compute_submission_id(BASE)}

        assert DuplicateTracker.from_sheet(FakeWriter()).is_duplicate(BASE)
