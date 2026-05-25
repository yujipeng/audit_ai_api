"""Common helper for dimension wrappers — shared snippet redaction + per-round
client call loop.

PRD AC-R2: ``hits[].snippet`` MUST NOT contain the api_key full value or its
first-8 characters. Pulled out here so every wrapper reuses the same redaction.
"""
from __future__ import annotations

from api_relay_audit.evaluator.thresholds import HITS_SNIPPET_MAX_CHARS


def redact_snippet(text: str, api_key: str, *, max_chars: int = HITS_SNIPPET_MAX_CHARS) -> str:
    """Trim ``text`` to ``max_chars`` and redact api_key + its 8-char prefix.

    PRD AC-R2: hits[].snippet must not echo the api_key (full or first-8).
    Replaces with ``<redacted>`` placeholder.
    """
    if not text:
        return ""
    s = text[:max_chars]
    if api_key:
        if api_key in s:
            s = s.replace(api_key, "<redacted-api-key>")
        if len(api_key) >= 8:
            prefix = api_key[:8]
            if prefix in s:
                s = s.replace(prefix, "<redacted>")
    return s
