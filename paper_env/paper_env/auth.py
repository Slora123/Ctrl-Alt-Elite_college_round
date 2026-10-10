"""Authentication / authorization seam.

paper_env stays self-contained: it only knows a ``Principal`` and an ``Authenticator`` protocol. The host application
(CrisisGrid's API) plugs in a real authenticator that verifies its signed tokens; standalone you can use
``StaticTokenAuthenticator`` (set ``PAPER_ENV_API_TOKEN``) or no authentication at all (loopback development).

Roles: ``viewer`` (read-only), ``researcher`` (create / edit / run own paper environments), ``admin`` (everything,
including other users' environments).
"""
from __future__ import annotations

import hmac
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Mapping, Protocol

ROLES = ("viewer", "researcher", "admin")


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    role: str = "researcher"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_write(self) -> bool:
        return self.role in ("researcher", "admin")


ANONYMOUS = Principal("local", "local", "admin")  # used only when no authenticator is configured
current_principal: ContextVar[Principal] = ContextVar("paper_env_principal", default=ANONYMOUS)


class Authenticator(Protocol):
    def authenticate(self, token: str) -> Principal | None: ...


class StaticTokenAuthenticator:
    """token -> principal. Constant-time comparison."""

    def __init__(self, tokens: Mapping[str, Principal]):
        self._tokens = dict(tokens)

    def authenticate(self, token: str) -> Principal | None:
        found = None
        for t, p in self._tokens.items():
            if hmac.compare_digest(t.encode(), token.encode()):
                found = p
        return found


def token_from(authorization: str | None, query_token: str | None) -> str | None:
    """Bearer header first, then ``?token=`` (browsers cannot set headers on WebSocket connections)."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return query_token or None
