"""OpenAI classification and summarisation.

Independently testable: construct a ``TriageClassifier`` with an explicit key
and model, or use ``from_env()``. Nothing here touches Sheets or Telegram.

The response format is enforced by the API, not requested in the prompt --
``TriageResult`` is converted to a JSON Schema and used to constrain decoding,
so an out-of-taxonomy category cannot be generated. See models.py.

Two backends
------------
The client ships against OpenAI, which is what ``responses.parse`` targets --
the current recommended API surface.

Setting ``OPENAI_BASE_URL`` switches to ``chat.completions.parse`` instead,
which is the endpoint every OpenAI-compatible provider implements (Gemini's
compatibility layer, Groq, Ollama, ...). ``/v1/responses`` is OpenAI-only, so
the fallback is what makes development against a free provider possible.

Both paths send the identical schema and return the identical validated
``TriageResult``; only the transport differs.
"""

from __future__ import annotations

import logging
import os

from openai import APIError, APITimeoutError, OpenAI, RateLimitError

from .models import Inquiry, TriageResult

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5-nano"

# Generous ceiling. gpt-5 models may spend reasoning tokens before producing
# the answer, and those count toward this limit -- too low a value yields a
# truncated (status="incomplete") response rather than an error.
MAX_OUTPUT_TOKENS = 2000

SYSTEM_PROMPT = """\
You triage inbound customer inquiries for a software company.

Assign exactly one category:
- Sales: pricing, plans, quotes, demos, upgrades, buying, contract terms.
- Technical Support: something is broken, erroring, misbehaving, or the \
customer cannot get a feature to work.
- Billing: invoices, charges, refunds, payment methods, subscription \
cancellation, incorrect amounts.
- General Question: anything else, including careers, partnerships, press, \
feedback, and questions about the company itself.

Disambiguation rules:
- A billing dispute about a charge is Billing, even if the customer is angry.
- "I want to cancel because the product is broken" is Technical Support; the \
underlying problem is technical.
- A question about what a plan includes, before purchase, is Sales. A question \
about what was actually charged, after purchase, is Billing.

Summarise neutrally and factually. Do not invent details that are not present \
in the message. Do not include the customer's name or any greeting.
"""


class ClassificationError(RuntimeError):
    """Raised when a usable TriageResult could not be obtained."""


class TriageClassifier:
    """Turns an ``Inquiry`` into a schema-validated ``TriageResult``."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")

        # The SDK retries 429s and 5xx itself with exponential backoff, so we
        # do not hand-roll a retry loop.
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout,
            max_retries=max_retries,
        )
        self.model = model
        self.base_url = base_url
        # Only genuine OpenAI serves /v1/responses.
        self._use_responses_api = not base_url

    @classmethod
    def from_env(cls) -> TriageClassifier:
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise ValueError("OPENAI_API_KEY is not set")
        model = (os.getenv("OPENAI_MODEL") or "").strip() or DEFAULT_MODEL
        base_url = (os.getenv("OPENAI_BASE_URL") or "").strip() or None
        return cls(api_key=api_key, model=model, base_url=base_url)

    @property
    def backend(self) -> str:
        """Human-readable description of which API surface is in use."""
        if self._use_responses_api:
            return "openai:/v1/responses"
        return f"compatible:/v1/chat/completions @ {self.base_url}"

    def classify(self, inquiry: Inquiry) -> TriageResult:
        """Classify one inquiry. Raises ClassificationError on any failure."""
        log.debug("Classifying inquiry from %s with %s", inquiry.email, self.model)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._render(inquiry)},
        ]

        try:
            if self._use_responses_api:
                result = self._via_responses(messages)
            else:
                result = self._via_chat_completions(messages)
        except (APITimeoutError, RateLimitError, APIError) as exc:
            # Retries are already exhausted by the time these escape.
            raise ClassificationError(f"OpenAI request failed: {exc}") from exc

        log.info(
            "Classified %s -> %s / %s",
            inquiry.email, result.category.value, result.priority.value,
        )
        return result

    # -- backends ----------------------------------------------------------

    def _via_responses(self, messages: list[dict]) -> TriageResult:
        response = self._client.responses.parse(
            model=self.model,
            input=messages,
            text_format=TriageResult,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        self._log_usage(getattr(response, "usage", None))

        # A safety refusal arrives as a distinct content type rather than as an
        # exception, so an unchecked `.output_parsed` would silently be None.
        for item in getattr(response, "output", None) or []:
            for content in getattr(item, "content", None) or []:
                if getattr(content, "type", None) == "refusal":
                    raise ClassificationError(
                        f"Model refused: {getattr(content, 'refusal', 'no reason')}"
                    )

        if getattr(response, "status", None) == "incomplete":
            reason = getattr(
                getattr(response, "incomplete_details", None), "reason", "unknown"
            )
            raise ClassificationError(f"Response was truncated ({reason})")

        result = getattr(response, "output_parsed", None)
        if result is None:
            raise ClassificationError("OpenAI returned no parsed result")
        return result

    def _via_chat_completions(self, messages: list[dict]) -> TriageResult:
        # No max_tokens here on purpose: OpenAI now wants max_completion_tokens
        # while several compatible providers still only accept max_tokens, and
        # sending the wrong one is a hard 400. The schema keeps output short
        # anyway, and this path is for free/local providers where the cost of
        # an unbounded response is nil.
        completion = self._client.chat.completions.parse(
            model=self.model,
            messages=messages,
            response_format=TriageResult,
        )
        self._log_usage(getattr(completion, "usage", None))

        if not completion.choices:
            raise ClassificationError("Provider returned no choices")

        choice = completion.choices[0]
        if getattr(choice.message, "refusal", None):
            raise ClassificationError(f"Model refused: {choice.message.refusal}")
        if choice.finish_reason == "length":
            raise ClassificationError("Response was truncated (length)")

        result = getattr(choice.message, "parsed", None)
        if result is None:
            raise ClassificationError(
                "Provider returned no parsed result -- it may not support "
                "schema-enforced structured outputs for this model"
            )
        return result

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _render(inquiry: Inquiry) -> str:
        # Labelled fields keep the customer's text from being mistaken for
        # instructions; the schema makes prompt injection largely inert anyway,
        # since the model cannot emit anything outside TriageResult.
        return (
            f"From: {inquiry.name} <{inquiry.email}>\n"
            f"Message:\n{inquiry.message}"
        )

    @staticmethod
    def _log_usage(usage: object) -> None:
        if usage is None:
            return
        log.debug(
            "tokens in=%s out=%s",
            getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", "?"),
            getattr(usage, "output_tokens", None)
            or getattr(usage, "completion_tokens", "?"),
        )
