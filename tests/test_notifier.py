"""Telegram message formatting.

Offline: ``format_message`` is a pure function, so escaping and truncation can
be verified without sending anything.

Every value interpolated into a message originates from an untrusted public
form, so unescaped markup is both a formatting bug and an injection vector.
"""

from __future__ import annotations

import pytest

from triagebot.models import Category, Inquiry, Priority, TriageResult
from triagebot.notifier import MAX_MESSAGE_CHARS, TelegramNotifier

HOSTILE = Inquiry(
    name="<script>alert('xss')</script> O'Brien & Sons",
    email="obrien+tag@example.co.uk",
    message="Our app crashed - please help. 5 < 10 & >20% failed!",
)

RESULT = TriageResult(
    summary="Customer reports a crash; 5 < 10 & >20% of runs failed.",
    category=Category.TECHNICAL_SUPPORT,
    priority=Priority.HIGH,
)


def format_hostile() -> str:
    return TelegramNotifier.format_message(HOSTILE, RESULT, "REF-1")


class TestEscaping:
    @pytest.mark.parametrize("dangerous", ["<script>", "</script>", "alert('xss')"])
    def test_raw_markup_does_not_survive(self, dangerous):
        assert dangerous not in format_hostile()

    @pytest.mark.parametrize("expected", ["&lt;script&gt;", "&amp;"])
    def test_dangerous_characters_appear_escaped(self, expected):
        assert expected in format_hostile()

    def test_only_our_own_tags_remain_as_markup(self):
        body = format_hostile()
        ours = sum(
            body.count(tag)
            for tag in ("<b>", "</b>", "<code>", "</code>", "<i>", "</i>")
        )
        assert body.count("<") == ours

    def test_markdown_metacharacters_are_left_alone(self):
        # These are special only in MarkdownV2, which is exactly why this
        # project uses HTML. Under MarkdownV2 an ordinary summary containing
        # "-" or "." fails to parse and the notification silently never
        # arrives; under HTML it passes through untouched.
        summary = "App crashed - please help. Costs rose 20% (see #4821)."
        body = TelegramNotifier.format_message(
            HOSTILE,
            TriageResult(
                summary=summary,
                category=Category.TECHNICAL_SUPPORT,
                priority=Priority.HIGH,
            ),
            "REF-1",
        )
        assert summary in body


class TestContent:
    def test_includes_the_triage_fields_a_reader_needs(self):
        body = TelegramNotifier.format_message(
            Inquiry(name="Ana Reyes", email="ana@example.com", message="hi"),
            RESULT,
            "REF-42",
        )
        assert "Ana Reyes" in body
        assert "ana@example.com" in body
        assert "High" in body
        assert "Technical Support" in body
        assert "REF-42" in body

    def test_stays_within_the_telegram_length_limit(self):
        huge = TriageResult(
            summary="x" * 5000,
            category=Category.GENERAL_QUESTION,
            priority=Priority.LOW,
        )
        body = TelegramNotifier.format_message(
            Inquiry(name="Long", email="l@example.com", message="hi"), huge, "REF-2"
        )
        assert len(body) < MAX_MESSAGE_CHARS


class TestConstruction:
    @pytest.mark.parametrize(
        "token,chat_id",
        [("", "123"), ("token", "")],
    )
    def test_rejects_empty_credentials(self, token, chat_id):
        with pytest.raises(ValueError):
            TelegramNotifier(bot_token=token, chat_id=chat_id)
