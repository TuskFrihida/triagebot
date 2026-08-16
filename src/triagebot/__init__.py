"""TriageBot: AI-assisted triage for customer inquiries.

Pipeline: inquiry -> OpenAI (summary/category/priority) -> Google Sheets -> Telegram.
Each integration lives in its own module and is independently testable.
"""

__version__ = "0.1.0"
