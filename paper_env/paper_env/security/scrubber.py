"""Redacts secrets from any text/JSON before it is stored, logged or emitted."""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Iterable, Mapping

REDACTED = "[REDACTED]"

# Known token shapes. Ordered roughly from most to least specific.
_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),                       # HuggingFace
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),                # GitHub
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),                     # OpenAI/Anthropic style
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                          # AWS access key id
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),              # Slack
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),           # user:pass@ in URLs
]
# key=value / "key": "value" where key looks secret-ish. Replaces only the value.
_KV = re.compile(
    r"""(?ix)
    (?P<k>["']?[\w.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|credential|private[_-]?key|access[_-]?key)[\w.-]*["']?
       \s*[:=]\s*)
    (?P<q>["']?)(?P<v>[^\s"',;]{6,})(?P=q)
    """
)
_SECRET_ENV_NAME = re.compile(r"(?i)(api[_-]?key|secret|token|passw(or)?d|credential|private[_-]?key|access[_-]?key)")
_MIN_SECRET_LEN = 6


class Scrubber:
    def __init__(self, secrets: Iterable[str] = (), scan_environ: bool = True):
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        self.add_secrets(secrets)
        if scan_environ:
            self.add_secrets(self.secret_values_from(os.environ))

    @staticmethod
    def secret_values_from(environ: Mapping[str, str]) -> list[str]:
        return [v for k, v in environ.items() if _SECRET_ENV_NAME.search(k) and len(v) >= _MIN_SECRET_LEN]

    def add_secrets(self, values: Iterable[str]) -> None:
        with self._lock:
            self._secrets.update(v for v in values if v and len(v) >= _MIN_SECRET_LEN)

    def scrub(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            known = sorted(self._secrets, key=len, reverse=True)
        for s in known:
            if s in text:
                text = text.replace(s, REDACTED)
        for p in _PATTERNS:
            text = p.sub(REDACTED, text)
        return _KV.sub(lambda m: f"{m.group('k')}{m.group('q')}{REDACTED}{m.group('q')}", text)

    _TOKENISH_TAIL = re.compile(r"[A-Za-z0-9_-]{8,}$")

    def might_end_mid_secret(self, text: str) -> bool:
        """True if ``text`` could be the beginning of a secret that has not fully arrived yet, so it must not be
        shown/stored until more data (or the end of the line) is known."""
        if self._TOKENISH_TAIL.search(text):
            return True
        with self._lock:
            secrets = list(self._secrets)
        for sec in secrets:
            for k in range(min(len(sec) - 1, len(text)), 0, -1):
                if text.endswith(sec[:k]):
                    return True
        return False

    def scrub_obj(self, obj: Any) -> Any:
        """Recursively scrub strings in JSON-like data (dict keys are kept)."""
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, Mapping):
            return {k: self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub_obj(v) for v in obj]
        return obj

    def subprocess_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Environment for child processes with secret-looking variables removed."""
        base = os.environ if base is None else base
        return {k: v for k, v in base.items() if not _SECRET_ENV_NAME.search(k)}
