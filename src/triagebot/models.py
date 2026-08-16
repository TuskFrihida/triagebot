"""Data models for TriageBot.

Two kinds live here:

* ``Inquiry``      -- what comes IN from the website form. Validated defensively,
                     because form data is untrusted user input.
* ``TriageResult`` -- what comes OUT of the model. This class is the contract
                     the OpenAI API enforces during generation: it is converted
                     to a JSON Schema and used to constrain decoding, so the
                     model physically cannot emit a category outside the enum.

Schema constraints imposed by OpenAI Structured Outputs (see
https://developers.openai.com/api/docs/guides/structured-outputs):

* every field must be required -- no Optional fields in ``TriageResult``
* every object must set ``additionalProperties: false`` -- done via
  ``extra="forbid"`` below
* string fields support only ``pattern`` and ``format``; ``maxLength`` is NOT
  supported, which is why summary length is steered by the prompt instead of
  by a ``Field(max_length=...)`` constraint that would be rejected.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# A deliberately permissive check. Full RFC 5322 validation needs the
# `email-validator` package, and a stricter regex would reject valid addresses
# for no benefit: we are not delivering mail, only recording the address the
# customer typed. Rejecting obvious junk is enough.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Bounds the cost and latency of a single classification. A pasted stack trace
# or a spam blast can be enormous; the first 4000 characters are more than
# enough to categorise an inquiry, and this caps worst-case token spend.
MAX_MESSAGE_CHARS = 4000


class Category(str, Enum):
    """The four categories the client specified. Exactly these, no others."""

    SALES = "Sales"
    TECHNICAL_SUPPORT = "Technical Support"
    BILLING = "Billing"
    GENERAL_QUESTION = "General Question"


class Priority(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class TriageResult(BaseModel):
    """AI-generated triage output. Schema-enforced by the OpenAI API."""

    # extra="forbid" is what emits `additionalProperties: false` into the JSON
    # Schema, which Structured Outputs requires.
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description=(
            "A one-or-two sentence neutral summary of what the customer wants. "
            "No greeting, no sign-off, no restating the customer's name."
        )
    )
    category: Category = Field(
        description="The single category that best fits the inquiry."
    )
    priority: Priority = Field(
        description=(
            "High if the customer is blocked, reports an outage or data loss, "
            "is being charged incorrectly, or is threatening to leave. "
            "Medium if they need a substantive answer to proceed. "
            "Low for general curiosity, feedback, or non-urgent questions."
        )
    )


class Inquiry(BaseModel):
    """A customer inquiry submitted through the website form.

    Validation is strict here on purpose: the client's acceptance criteria
    require that invalid or incomplete submissions are handled without
    crashing, and the cheapest place to reject bad data is before it reaches
    a paid API call.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str
    email: str
    message: str

    @field_validator("name", "message")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        # Pydantic strips whitespace before this runs, so a field of only
        # spaces arrives here as "".
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            raise ValueError(f"{value!r} is not a valid email address")
        return value

    @field_validator("message")
    @classmethod
    def _truncate(cls, value: str) -> str:
        if len(value) > MAX_MESSAGE_CHARS:
            return value[:MAX_MESSAGE_CHARS] + " [truncated]"
        return value
