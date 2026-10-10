from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..config import Settings
from .deps import AuthDeps, http_error
from .models import User
from .service import AuthError, AuthService


class Register(BaseModel):
    email: str
    name: str = ""
    password: str


class Login(BaseModel):
    email: str
    password: str


class ChangePassword(BaseModel):
    current_password: str
    new_password: str


class NewToken(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expires_days: int | None = Field(default=None, ge=1, le=3650)


class UpdateUser(BaseModel):
    role: str | None = None
    disabled: bool | None = None


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def build_auth_router(svc: AuthService, deps: AuthDeps, settings: Settings) -> APIRouter:
    r = APIRouter(prefix="/auth", tags=["auth"])

    def session(user: User, token: str, exp: Any) -> dict[str, Any]:
        return {"token": token, "token_type": "bearer", "expires_at": exp.isoformat(), "user": user.public()}

    @r.get("/config")
    def config() -> dict[str, Any]:
        """Public: lets the sign-in page know what to offer."""
        return {"auth_enabled": settings.auth_enabled, "registration_open": settings.allow_registration or svc.repo.count_users() == 0,
                "first_user_becomes_admin": svc.repo.count_users() == 0}

    @r.post("/register", status_code=201)
    def register(body: Register, request: Request) -> dict[str, Any]:
        try:
            return session(*svc.register(body.email, body.name, body.password, _ip(request)))
        except AuthError as e:
            raise http_error(e) from e

    @r.post("/login")
    def login(body: Login, request: Request) -> dict[str, Any]:
        try:
            return session(*svc.login(body.email, body.password, _ip(request)))
        except AuthError as e:
            raise http_error(e) from e

    @r.get("/me")
    def me(user: User = Depends(deps.current_user)) -> dict[str, Any]:
        return user.public()

    @r.post("/change-password")
    def change_password(body: ChangePassword, user: User = Depends(deps.current_user)) -> dict[str, Any]:
        try:
            token, exp = svc.change_password(user, body.current_password, body.new_password)
        except AuthError as e:
            raise http_error(e) from e
        return {"token": token, "expires_at": exp.isoformat()}

    @r.post("/logout-all", status_code=204)
    def logout_all(user: User = Depends(deps.current_user)) -> None:
        """Invalidate every session token this user has (API tokens are unaffected; revoke those individually)."""
        svc.logout_all(user)

    # ---- API tokens
    @r.get("/tokens")
    def list_tokens(user: User = Depends(deps.current_user)) -> list[dict[str, Any]]:
        return [t.model_dump(mode="json") for t in svc.repo.list_tokens(user.id)]

    @r.post("/tokens", status_code=201)
    def create_token(body: NewToken, user: User = Depends(deps.writer)) -> dict[str, Any]:
        try:
            tok, plain = svc.create_api_token(user, body.name, body.expires_days)
        except AuthError as e:
            raise http_error(e) from e
        return {**tok.model_dump(mode="json"), "token": plain, "note": "copy it now; it cannot be shown again"}

    @r.delete("/tokens/{token_id}", status_code=204)
    def revoke_token(token_id: str, user: User = Depends(deps.current_user)) -> None:
        from fastapi import HTTPException
        if not svc.repo.revoke_token(user.id, token_id):
            raise HTTPException(404, "no such token")

    # ---- administration
    @r.get("/users")
    def users(_: User = Depends(deps.admin)) -> list[dict[str, Any]]:
        return [u.public() for u in svc.repo.list_users()]

    @r.patch("/users/{user_id}")
    def update_user(user_id: str, body: UpdateUser, admin: User = Depends(deps.admin)) -> dict[str, Any]:
        try:
            return svc.update_user(admin, user_id, role=body.role, disabled=body.disabled).public()
        except AuthError as e:
            raise http_error(e) from e

    return r
