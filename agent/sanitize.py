"""Sanitization for untrusted free text (reviews etc.).

Free text from tools is DATA. It is quoted and tagged before it ever reaches a
model prompt, a trace, or the user, and it is never parsed for instructions.
"""
import re

_SECRET = re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*\S+")


def quote_untrusted(text):
    """Wrap free text so downstream readers treat it as inert data."""
    redacted = _SECRET.sub("[redacted-secret]", str(text))
    return {"type": "untrusted_text", "content": redacted}


def plain(quoted):
    """Plain-text rendering for CLI display, visibly marked as provider-supplied."""
    return f"[provider review] {quoted['content']}"
