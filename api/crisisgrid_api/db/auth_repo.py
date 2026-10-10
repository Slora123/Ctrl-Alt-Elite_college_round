from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from ..auth.models import ApiToken, User
from .base import SqlRepository
from .tables import ApiTokenRow, UserRow


def _user(r: UserRow) -> User:
    return User(id=r.id, email=r.email, name=r.name, role=r.role, disabled=r.disabled, token_version=r.token_version,
                created_at=r.created_at, last_login_at=r.last_login_at)


def _token(r: ApiTokenRow) -> ApiToken:
    return ApiToken(id=r.id, user_id=r.user_id, name=r.name, prefix=r.prefix, created_at=r.created_at,
                    last_used_at=r.last_used_at, expires_at=r.expires_at, revoked=r.revoked)


class SqlAuthRepo(SqlRepository):
    # ---- users
    def count_users(self) -> int:
        with self._scope() as s:
            return int(s.scalar(select(func.count()).select_from(UserRow)) or 0)

    def add_user(self, user: User, password_hash: str) -> None:
        with self._scope() as s:
            s.add(UserRow(id=user.id, email=user.email, name=user.name, password_hash=password_hash, role=user.role,
                          disabled=user.disabled, token_version=user.token_version, created_at=user.created_at))

    def get_user(self, user_id: str) -> User | None:
        with self._scope() as s:
            r = s.get(UserRow, user_id)
            return None if r is None else _user(r)

    def get_by_email(self, email: str) -> tuple[User, str, int, datetime | None] | None:
        """(user, password_hash, failed_logins, locked_until)"""
        with self._scope() as s:
            r = s.scalar(select(UserRow).where(UserRow.email == email))
            return None if r is None else (_user(r), r.password_hash, r.failed_logins, r.locked_until)

    def list_users(self) -> list[User]:
        with self._scope() as s:
            return [_user(r) for r in s.scalars(select(UserRow).order_by(UserRow.created_at))]

    def record_login_failure(self, user_id: str, failed: int, locked_until: datetime | None) -> None:
        with self._scope() as s:
            r = s.get(UserRow, user_id)
            if r:
                r.failed_logins, r.locked_until = failed, locked_until

    def record_login_success(self, user_id: str, when: datetime) -> None:
        with self._scope() as s:
            r = s.get(UserRow, user_id)
            if r:
                r.failed_logins, r.locked_until, r.last_login_at = 0, None, when

    def update_user(self, user_id: str, *, role: str | None = None, disabled: bool | None = None, name: str | None = None,
                    password_hash: str | None = None, bump_token_version: bool = False) -> User | None:
        with self._scope() as s:
            r = s.get(UserRow, user_id)
            if r is None:
                return None
            if role is not None:
                r.role = role
            if disabled is not None:
                r.disabled = disabled
            if name is not None:
                r.name = name
            if password_hash is not None:
                r.password_hash = password_hash
            if bump_token_version:
                r.token_version += 1
            return _user(r)

    def count_admins(self) -> int:
        with self._scope() as s:
            return int(s.scalar(select(func.count()).select_from(UserRow).where(UserRow.role == "admin", UserRow.disabled.is_(False))) or 0)

    # ---- api tokens
    def add_token(self, token: ApiToken, token_hash: str) -> None:
        with self._scope() as s:
            s.add(ApiTokenRow(id=token.id, user_id=token.user_id, name=token.name, token_hash=token_hash, prefix=token.prefix,
                              created_at=token.created_at, expires_at=token.expires_at, revoked=False))

    def token_by_hash(self, token_hash: str) -> ApiToken | None:
        with self._scope() as s:
            r = s.scalar(select(ApiTokenRow).where(ApiTokenRow.token_hash == token_hash))
            return None if r is None else _token(r)

    def touch_token(self, token_id: str, when: datetime) -> None:
        with self._scope() as s:
            r = s.get(ApiTokenRow, token_id)
            if r:
                r.last_used_at = when

    def list_tokens(self, user_id: str) -> list[ApiToken]:
        with self._scope() as s:
            return [_token(r) for r in s.scalars(select(ApiTokenRow).where(ApiTokenRow.user_id == user_id).order_by(ApiTokenRow.created_at))]

    def revoke_token(self, user_id: str, token_id: str) -> bool:
        with self._scope() as s:
            r = s.get(ApiTokenRow, token_id)
            if r is None or r.user_id != user_id:
                return False
            r.revoked = True
            return True
