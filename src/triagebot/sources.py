"""Inquiry ingress.

This module is the seam between "where inquiries come from" and "what we do
with them". The pipeline consumes plain dictionaries, so adding a new source
-- an HTTP webhook, a mailbox poller, a Google Forms responses tab -- means
adding one function here and changing nothing else.

Records are returned unvalidated on purpose. Validation belongs to the
pipeline, which must report a malformed submission and carry on rather than
aborting the whole batch.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """Raised when inquiries could not be read at all."""


def from_json_file(path: str | Path) -> list[dict]:
    """Read inquiries from a JSON file.

    Accepts either a list of objects or a single object, since a form export
    and a single test submission are both reasonable things to hand this.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise SourceError(f"Input file not found: {file_path}")

    try:
        # utf-8-sig strips a byte-order mark if present and behaves exactly
        # like utf-8 otherwise. Windows tools (PowerShell, Excel, Notepad)
        # routinely write a BOM, and json.loads rejects it outright.
        payload = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SourceError(f"{file_path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise SourceError(f"Could not read {file_path}: {exc}") from exc

    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise SourceError(
            f"{file_path} must contain a JSON array of inquiries or a single "
            f"inquiry object, found {type(payload).__name__}"
        )

    # Non-dict entries are kept rather than dropped here: the pipeline reports
    # each one as an invalid submission, which is more useful to the operator
    # than silently shrinking the batch.
    log.info("Loaded %d record(s) from %s", len(payload), file_path)
    return payload
