"""Google Sheets persistence.

Independently testable: construct a ``SheetsWriter`` with an explicit key file
and sheet id, or use ``from_env()``. Nothing here touches OpenAI or Telegram.

Authentication uses a service account -- a robot Google identity with its own
email address, which must be granted Editor access on the target spreadsheet
like any other collaborator. A valid key with no share grant produces a 403,
not an auth error, which is the single most common setup mistake here.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import gspread
from gspread.exceptions import APIError, WorksheetNotFound

from .models import Inquiry, TriageResult

log = logging.getLogger(__name__)

DEFAULT_WORKSHEET = "Inquiries"

HEADERS = [
    "Timestamp (UTC)",
    "Submission ID",
    "Name",
    "Email",
    "Message",
    "Summary",
    "Category",
    "Priority",
]

# Column index (1-based) of Submission ID, used for the dedupe lookup.
_SUBMISSION_ID_COL = 2

# RAW stores exactly what we send. USER_ENTERED would evaluate a leading "="
# as a formula, so a hostile form submission could plant a live formula in the
# client's spreadsheet (CSV/formula injection). Inquiries come from an
# untrusted public form, so this must never be changed to USER_ENTERED.
_VALUE_INPUT_OPTION = "RAW"


class SheetsError(RuntimeError):
    """Raised when the spreadsheet could not be read or written."""


class SheetsWriter:
    """Appends triaged inquiries to a Google Sheet."""

    def __init__(
        self,
        service_account_file: str | Path,
        sheet_id: str,
        worksheet_name: str = DEFAULT_WORKSHEET,
    ) -> None:
        self.key_path = Path(service_account_file)
        if not self.key_path.is_file():
            raise SheetsError(f"Service-account key not found: {self.key_path}")
        if not sheet_id:
            raise SheetsError("sheet_id must not be empty")

        self.sheet_id = sheet_id
        self.worksheet_name = worksheet_name
        self._worksheet: gspread.Worksheet | None = None

    @classmethod
    def from_env(cls) -> SheetsWriter:
        key_file = (os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
        sheet_id = (os.getenv("GOOGLE_SHEET_ID") or "").strip()
        if not key_file:
            raise SheetsError("GOOGLE_SERVICE_ACCOUNT_FILE is not set")
        if not sheet_id:
            raise SheetsError("GOOGLE_SHEET_ID is not set")

        path = Path(key_file)
        if not path.is_absolute():
            # Paths in .env are written relative to the project root, which is
            # two levels up from this file (src/triagebot/sheets.py).
            path = Path(__file__).resolve().parent.parent.parent / path

        return cls(
            service_account_file=path,
            sheet_id=sheet_id,
            worksheet_name=(os.getenv("GOOGLE_WORKSHEET_NAME") or "").strip()
            or DEFAULT_WORKSHEET,
        )

    # -- connection --------------------------------------------------------

    @property
    def worksheet(self) -> gspread.Worksheet:
        """The target worksheet, opened and created on first use."""
        if self._worksheet is None:
            self._worksheet = self._open_or_create()
        return self._worksheet

    def _open_or_create(self) -> gspread.Worksheet:
        try:
            client = gspread.service_account(filename=str(self.key_path))
            spreadsheet = client.open_by_key(self.sheet_id)
        except APIError as exc:
            raise SheetsError(self._explain(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - malformed key file, etc.
            raise SheetsError(f"Could not open spreadsheet: {exc}") from exc

        try:
            worksheet = spreadsheet.worksheet(self.worksheet_name)
            log.debug("Opened worksheet %r", self.worksheet_name)
        except WorksheetNotFound:
            # Creating the tab means the client can point this at a blank
            # spreadsheet with no manual setup step.
            log.info("Worksheet %r not found; creating it", self.worksheet_name)
            try:
                worksheet = spreadsheet.add_worksheet(
                    title=self.worksheet_name, rows=1000, cols=len(HEADERS)
                )
            except APIError as exc:
                raise SheetsError(self._explain(exc)) from exc
        except APIError as exc:
            raise SheetsError(self._explain(exc)) from exc

        # Ensure headers on every open, not only on creation: a tab that
        # already exists but is empty (created by hand, or left over from a
        # partial run) would otherwise never get a header row.
        self._ensure_headers(worksheet)
        return worksheet

    def _ensure_headers(self, worksheet: gspread.Worksheet) -> None:
        try:
            first_row = worksheet.row_values(1)
        except APIError as exc:
            raise SheetsError(self._explain(exc)) from exc

        if first_row == HEADERS:
            return

        if first_row:
            # Populated but different: refuse rather than silently appending
            # rows whose columns do not line up with the existing header.
            raise SheetsError(
                f"Worksheet {worksheet.title!r} has an unexpected header row.\n"
                f"  found:    {first_row}\n"
                f"  expected: {HEADERS}\n"
                f"Rename or clear the tab, or point GOOGLE_WORKSHEET_NAME "
                f"somewhere else."
            )

        log.info("Writing header row to %r", worksheet.title)
        try:
            worksheet.insert_row(
                HEADERS, index=1, value_input_option=_VALUE_INPUT_OPTION
            )
            worksheet.freeze(rows=1)
            worksheet.format(
                f"A1:{chr(ord('A') + len(HEADERS) - 1)}1",
                {"textFormat": {"bold": True}},
            )
        except APIError as exc:
            raise SheetsError(self._explain(exc)) from exc

    # -- operations --------------------------------------------------------

    def append(
        self,
        inquiry: Inquiry,
        result: TriageResult,
        submission_id: str,
        timestamp: datetime | None = None,
    ) -> None:
        """Append one triaged inquiry as a single row."""
        when = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")
        row = [
            when,
            submission_id,
            inquiry.name,
            inquiry.email,
            inquiry.message,
            result.summary,
            result.category.value,
            result.priority.value,
        ]
        try:
            self.worksheet.append_row(row, value_input_option=_VALUE_INPUT_OPTION)
        except APIError as exc:
            raise SheetsError(self._explain(exc)) from exc

        log.info("Wrote %s to sheet (%s)", submission_id, result.category.value)

    def existing_submission_ids(self) -> set[str]:
        """Submission IDs already recorded, for duplicate suppression.

        The sheet is the source of truth rather than a local state file, so
        dedupe survives restarts and works from any machine.
        """
        try:
            column = self.worksheet.col_values(_SUBMISSION_ID_COL)
        except APIError as exc:
            raise SheetsError(self._explain(exc)) from exc

        # Drop the header cell and any blank trailing cells.
        return {value for value in column[1:] if value}

    # -- helpers -----------------------------------------------------------

    def _explain(self, exc: APIError) -> str:
        """Turn a raw Google API error into something actionable."""
        text = str(exc)
        if "PERMISSION_DENIED" in text or "403" in text:
            return (
                f"Permission denied on spreadsheet {self.sheet_id}. Share it as "
                f"Editor with the service account listed in {self.key_path.name} "
                f"(the 'client_email' field). Original error: {text}"
            )
        if "404" in text or "NOT_FOUND" in text:
            return (
                f"Spreadsheet {self.sheet_id} not found. Check GOOGLE_SHEET_ID "
                f"matches the id in the sheet URL. Original error: {text}"
            )
        if "429" in text or "RESOURCE_EXHAUSTED" in text:
            return f"Google Sheets rate limit hit. Original error: {text}"
        return f"Google Sheets API error: {text}"
