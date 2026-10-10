from __future__ import annotations

import os
from typing import Mapping, Protocol


class SecretsProvider(Protocol):
    """Source of credentials. Values are read only at the moment they are needed
    (e.g. dataset fetch) and are never persisted or handed to cells."""

    def get(self, name: str) -> str | None: ...
    def known_values(self) -> list[str]: ...  # so the scrubber can redact them


class EnvVarSecretsProvider:
    """Reads secrets from environment variables, optionally via a name->env var mapping."""

    def __init__(self, mapping: Mapping[str, str] | None = None, environ: Mapping[str, str] | None = None):
        self._mapping = dict(mapping or {})
        self._environ = environ if environ is not None else os.environ

    def get(self, name: str) -> str | None:
        return self._environ.get(self._mapping.get(name, name)) or None

    def known_values(self) -> list[str]:
        names = set(self._mapping.values()) | set(self._mapping)
        return [v for n in names if (v := self._environ.get(n))]
