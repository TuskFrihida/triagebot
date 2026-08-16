"""Duplicate submission suppression.

Two submissions are the same if they carry the same email address and the same
message body. That definition is deliberately conservative, because the two
failure modes are not symmetric:

* processing a duplicate costs a fraction of a cent and one redundant
  notification;
* suppressing a genuine inquiry costs the client a customer.

So we only suppress when the content really is identical. Identity by email
alone is rejected outright -- it would silently swallow a customer's second,
different question.

The check is designed to run *before* classification, so a duplicate costs no
API call at all.

Independently testable: ``DuplicateTracker`` takes any iterable of known ids,
so it can be exercised with no network access. ``from_sheet`` is a convenience
for wiring it to the real source of truth.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from typing import Iterable

from .models import Inquiry

log = logging.getLogger(__name__)

# 64 bits of SHA-256. At this volume the collision probability is negligible
# (a coincidence would need billions of inquiries), and a short id keeps the
# spreadsheet column readable for the client.
_ID_LENGTH = 16

_WHITESPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Reduce cosmetic differences that do not change meaning.

    NFC composition means the same characters typed on different platforms
    hash identically, and collapsing whitespace absorbs stray newlines
    introduced by a form or an email client. Case in the message body is left
    alone: rewriting it would widen what counts as a duplicate, and the bias
    here is toward processing twice rather than dropping a real inquiry.
    """
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def compute_submission_id(inquiry: Inquiry) -> str:
    """Stable id for an inquiry, derived only from its content.

    Deterministic across runs and machines, so the same submission always
    produces the same id and no state has to be carried between processes.
    """
    email = _normalise(inquiry.email).casefold()  # addresses are case-insensitive
    message = _normalise(inquiry.message)

    # A null separator cannot appear in either field, so distinct pairs cannot
    # be concatenated into the same string.
    payload = f"{email}\x00{message}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:_ID_LENGTH]


class DuplicateTracker:
    """Remembers which submissions have already been processed."""

    def __init__(self, known_ids: Iterable[str] = ()) -> None:
        self._seen: set[str] = {i for i in known_ids if i}

    @classmethod
    def from_sheet(cls, writer) -> DuplicateTracker:
        """Seed from the spreadsheet, the source of truth.

        Reading ids back from the sheet rather than a local state file means
        deduplication survives restarts, works from any machine, and cannot
        drift out of sync with what was actually recorded.
        """
        ids = writer.existing_submission_ids()
        log.info("Loaded %d previously processed submission id(s)", len(ids))
        return cls(ids)

    def __len__(self) -> int:
        return len(self._seen)

    def __contains__(self, submission_id: str) -> bool:
        return submission_id in self._seen

    def is_duplicate(self, inquiry: Inquiry) -> bool:
        """True if this exact submission has already been processed."""
        return compute_submission_id(inquiry) in self._seen

    def remember(self, submission_id: str) -> None:
        """Record an id as processed.

        Called after a successful write so that duplicates appearing later in
        the same batch are caught without re-reading the sheet.
        """
        self._seen.add(submission_id)
