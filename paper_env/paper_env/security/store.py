"""Per-user secrets (the "key" icon in Colab). Values are encrypted at rest (Fernet) in a file outside the database,
never returned by any API (names and metadata only), registered with the scrubber so they cannot reach outputs, logs,
events or the database, and only handed to a notebook if the user explicitly granted that secret to cells.

Keep the key safe: ``PAPER_ENV_SECRET_KEY`` (urlsafe base64, 32 bytes) or an auto-generated ``secrets.key`` (0600) next
to the store. Losing the key makes stored secrets unreadable (they must be re-entered).
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..auth import current_principal
from .secrets import SecretsProvider

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_VALUE = 16_384


class SecretError(ValueError):
    pass


class SecretsStore:
    def __init__(self, directory: Path, key: str | None = None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._file = self.dir / "secrets.enc"
        self._lock = threading.RLock()
        self._fernet = Fernet(self._load_key(key))

    def _load_key(self, explicit: str | None) -> bytes:
        env = explicit or os.environ.get("PAPER_ENV_SECRET_KEY")
        if env:
            return env.encode()
        kf = self.dir / "secrets.key"
        if not kf.exists():
            fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        return kf.read_bytes().strip()

    def _read(self) -> dict[str, Any]:
        if not self._file.exists():
            return {}
        try:
            return json.loads(self._fernet.decrypt(self._file.read_bytes()))  # type: ignore[no-any-return]
        except (InvalidToken, ValueError) as e:
            raise SecretError("the secrets file cannot be decrypted (wrong or missing key)") from e

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self._file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(self._fernet.encrypt(json.dumps(data).encode()))
        tmp.replace(self._file)

    # ---- API ------------------------------------------------------------------------------------------------
    def put(self, owner: str, name: str, value: str, *, allow_in_cells: bool = False) -> dict[str, Any]:
        if not _NAME.match(name):
            raise SecretError("secret names must be identifiers (letters, digits, underscore; max 64)")
        if not value or len(value) > MAX_VALUE:
            raise SecretError(f"secret value must be 1..{MAX_VALUE} characters")
        with self._lock:
            data = self._read()
            data.setdefault(owner, {})[name] = {"value": value, "allow_in_cells": bool(allow_in_cells),
                                                "updated_at": datetime.now(timezone.utc).isoformat()}
            self._write(data)
        return self.meta(owner, name)

    def set_grant(self, owner: str, name: str, allow_in_cells: bool) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            if name not in data.get(owner, {}):
                raise KeyError(name)
            data[owner][name]["allow_in_cells"] = bool(allow_in_cells)
            self._write(data)
        return self.meta(owner, name)

    def delete(self, owner: str, name: str) -> None:
        with self._lock:
            data = self._read()
            if name not in data.get(owner, {}):
                raise KeyError(name)
            del data[owner][name]
            self._write(data)

    def meta(self, owner: str, name: str) -> dict[str, Any]:
        e = self._read().get(owner, {})[name]
        return {"name": name, "allow_in_cells": e["allow_in_cells"], "updated_at": e["updated_at"]}

    def list(self, owner: str) -> list[dict[str, Any]]:
        return sorted(({"name": n, "allow_in_cells": e["allow_in_cells"], "updated_at": e["updated_at"]}
                       for n, e in self._read().get(owner, {}).items()), key=lambda x: x["name"])

    def get(self, owner: str, name: str) -> str | None:
        e = self._read().get(owner, {}).get(name)
        return e["value"] if e else None

    def granted_to_cells(self, owner: str) -> dict[str, str]:
        return {n: e["value"] for n, e in self._read().get(owner, {}).items() if e["allow_in_cells"]}

    def all_values(self) -> list[str]:
        return [e["value"] for owner in self._read().values() for e in owner.values()]


class ChainedSecretsProvider:
    """Stored per-user secrets first (for the current request's user), then the fallback (environment variables).
    Plugs into the existing ``SecretsProvider`` interface, so dataset fetchers need no change."""

    def __init__(self, store: SecretsStore, fallback: SecretsProvider):
        self.store, self.fallback = store, fallback

    def get(self, name: str) -> str | None:
        try:
            v = self.store.get(current_principal.get().id, name)
        except SecretError:
            v = None
        return v if v is not None else self.fallback.get(name)

    def known_values(self) -> list[str]:
        try:
            stored = self.store.all_values()
        except SecretError:
            stored = []
        return stored + self.fallback.known_values()
