"""FastAPI dependencies. With ``CG_AUTH_ENABLED=0`` (tests, single-user local use) everything runs as a built-in admin."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from ..config import Settings
from .models import User
from .service import AuthError, AuthService

LOCAL_ADMIN = User(id="local", email="local@localhost", name="local", role="admin")


class AuthDeps:
    def __init__(self, service: AuthService, settings: Settings):
        self.service, self.settings = service, settings
        # bind as plain functions so FastAPI can use them in Depends(...)
        self.current_user = self._current_user
        self.writer = self.current_user  # replaced by build_deps
        self.admin = self.current_user

    def _current_user(self, request: Request) -> User:
        if not self.settings.auth_enabled:
            return LOCAL_ADMIN
        h = request.headers.get("authorization", "")
        token = h[7:].strip() if h.lower().startswith("bearer ") else None
        # EventSource cannot set headers, so the SSE stream alone also accepts ?token=
        if token is None and request.method == "GET" and request.url.path.endswith("/events"):
            token = request.query_params.get("token")
        user = self.service.authenticate(token) if token else None
        if user is None:
            raise HTTPException(401, "missing or invalid credentials", headers={"WWW-Authenticate": "Bearer"})
        return user


def build_deps(service: AuthService, settings: Settings) -> AuthDeps:
    d = AuthDeps(service, settings)

    def writer(user: User = Depends(d.current_user)) -> User:
        if not user.can_write:
            raise HTTPException(403, "your role is read-only")
        return user

    def admin(user: User = Depends(d.current_user)) -> User:
        if not user.is_admin:
            raise HTTPException(403, "administrator only")
        return user

    d.writer, d.admin = writer, admin
    return d


def http_error(e: AuthError) -> HTTPException:
    return HTTPException(e.status, str(e))
