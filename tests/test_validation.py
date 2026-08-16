"""Input validation and ingress.

Entirely offline. Malformed submissions are an expected condition for a public
web form, so these tests assert they are rejected cleanly rather than crashing.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from triagebot.models import MAX_MESSAGE_CHARS, Inquiry
from triagebot.sources import SourceError, from_json_file


class TestInquiry:
    def test_accepts_a_well_formed_submission(self):
        inquiry = Inquiry(
            name="Ana Reyes", email="ana@example.com", message="Help please"
        )
        assert inquiry.name == "Ana Reyes"

    def test_strips_surrounding_whitespace(self):
        inquiry = Inquiry(
            name="  Ana Reyes  ", email=" ana@example.com ", message="  Help  "
        )
        assert inquiry.name == "Ana Reyes"
        assert inquiry.email == "ana@example.com"
        assert inquiry.message == "Help"

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"name": "  ", "email": "a@b.co", "message": "hi"},
                         id="blank name"),
            pytest.param({"name": "Ana", "email": "a@b.co", "message": "   "},
                         id="blank message"),
            pytest.param({"name": "Ana", "email": "nope", "message": "hi"},
                         id="email without @"),
            pytest.param({"name": "Ana", "email": "ana@localhost", "message": "hi"},
                         id="email without TLD"),
            pytest.param({"name": "Ana", "email": "a b@c.co", "message": "hi"},
                         id="email with a space"),
            pytest.param({"name": "Ana", "email": "a@b.co"}, id="message missing"),
            pytest.param({"email": "a@b.co", "message": "hi"}, id="name missing"),
        ],
    )
    def test_rejects_malformed_submissions(self, payload):
        with pytest.raises(ValidationError):
            Inquiry(**payload)

    def test_ignores_unknown_fields(self):
        # Form payloads routinely carry extra keys such as a honeypot field or
        # a CSRF token; they must not cause a rejection.
        inquiry = Inquiry(
            name="Ana", email="a@b.co", message="hi", _comment="ignore me"
        )
        assert inquiry.name == "Ana"

    def test_truncates_oversized_messages_rather_than_rejecting(self):
        # Bounding token spend must not come at the cost of losing an inquiry.
        inquiry = Inquiry(name="Ana", email="a@b.co", message="x" * 50_000)
        assert inquiry.message.endswith("[truncated]")
        assert len(inquiry.message) < MAX_MESSAGE_CHARS + 100

    def test_preserves_non_latin_text_and_emoji(self):
        message = "請求書が届きません 😟"
        assert Inquiry(name="Yuki", email="y@e.jp", message=message).message == message


class TestJsonSource:
    def _write(self, tmp_path, content, encoding="utf-8"):
        path = tmp_path / "inquiries.json"
        path.write_text(content, encoding=encoding)
        return path

    def test_reads_a_list(self, tmp_path):
        path = self._write(tmp_path, json.dumps([{"email": "a@b.co"}]))
        assert from_json_file(path) == [{"email": "a@b.co"}]

    def test_wraps_a_single_object(self, tmp_path):
        path = self._write(tmp_path, json.dumps({"email": "a@b.co"}))
        assert from_json_file(path) == [{"email": "a@b.co"}]

    def test_reads_a_file_with_a_byte_order_mark(self, tmp_path):
        # PowerShell, Excel and Notepad all write a BOM; json.loads rejects it.
        path = self._write(tmp_path, json.dumps([{"email": "a@b.co"}]),
                           encoding="utf-8-sig")
        assert from_json_file(path) == [{"email": "a@b.co"}]

    def test_keeps_non_object_entries_for_the_pipeline_to_report(self, tmp_path):
        # Dropping them here would silently shrink the batch; the operator is
        # better served by an explicit "invalid" outcome per record.
        path = self._write(tmp_path, json.dumps([{"email": "a@b.co"}, "junk"]))
        assert from_json_file(path) == [{"email": "a@b.co"}, "junk"]

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(SourceError, match="not found"):
            from_json_file(tmp_path / "nope.json")

    def test_rejects_invalid_json(self, tmp_path):
        path = self._write(tmp_path, "{not json")
        with pytest.raises(SourceError, match="not valid JSON"):
            from_json_file(path)

    def test_rejects_a_bare_scalar_document(self, tmp_path):
        path = self._write(tmp_path, "42")
        with pytest.raises(SourceError, match="must contain"):
            from_json_file(path)
