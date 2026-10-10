"""Secret scrubbing for anything that leaves a worker or reaches storage (logs, outputs, events, DB).

Two layers: exact redaction of the secret values we know about, and pattern redaction for credentials we do not (a key a
notebook printed, a token in a traceback). Over-redacting is acceptable; leaking is not.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = ["scrub", "find_credentials", "REDACTED"]

REDACTED = "[REDACTED]"

_PATTERNS = [
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),                      # Hugging Face
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),               # GitHub
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}\b"),          # OpenAI / Anthropic style
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                         # AWS access key id
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),             # Slack
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),       # Authorization headers
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b(\s*[=:]\s*)(['\"]?)[^\s'\"]{8,}\3"),
]


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact known secret values (any length >= 4) and credential-shaped strings."""
    for s in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(s, REDACTED)
    for rx in _PATTERNS[:-1]:
        text = rx.sub(REDACTED, text)
    return _PATTERNS[-1].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def find_credentials(text: str) -> list[str]:
    """Credential-shaped strings that are unmistakable (provider key formats, bearer tokens, private keys). Deliberately
    excludes the loose `password = "..."` heuristic, which scrub() applies to output but which would reject too much code."""
    found: list[str] = []
    for rx in _PATTERNS[:-1]:
        found += [m.group(0) for m in rx.finditer(text)]
    return found
