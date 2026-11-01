"""Sanitization for untrusted free text (reviews etc.).

Free text from tools is DATA. It is quoted and tagged before it ever reaches a
model prompt, a trace, or the user, and it is never parsed for instructions.
Secrets are redacted and terminal control characters are stripped, so hostile
text cannot leak credentials or hijack the user's display.
"""
import re

_SECRET = re.compile(
    r"(api[_-]?key|password|secret|token|passwd|credential|private[_-]?key)"
    r"\s*[:=]\s*\S+"
    r"|AKIA[0-9A-Z]{16}"                                   # AWS access key id
    r"|bearer\s+[A-Za-z0-9._\-]{8,}"                # bearer tokens
    r"|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_CONTROL = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f"
    r"\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"  # zero-width + bidi overrides
)


def redact_secrets(text):
    """Redact credential-shaped patterns from any string (used for traces)."""
    return _SECRET.sub("[redacted-secret]", str(text))


def contains_secret(text):
    """True if the text carries a credential-shaped pattern."""
    return bool(_SECRET.search(str(text)))


def quote_untrusted(text):
    """Wrap free text so downstream readers treat it as inert data."""
    redacted = _SECRET.sub("[redacted-secret]", str(text))
    cleaned = _CONTROL.sub("\uFFFD", redacted)
    return {"type": "untrusted_text", "content": cleaned}


def clean_display(text):
    """Strip terminal-control characters from any string shown to the user
    (provider names, memory values, ...). Newlines are flattened by callers
    where a single line is required."""
    return _CONTROL.sub("\uFFFD", str(text))


def plain(quoted):
    """Plain-text rendering for CLI display, visibly marked as provider-supplied."""
    return f"[provider review] {quoted['content']}"
