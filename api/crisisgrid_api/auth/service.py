from __future__ import annotations

import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Settings
from ..db.auth_repo import SqlAuthRepo
from .models import ROLES, ApiToken, User, utcnow
from .security import (
    API_TOKEN_PREFIX, DUMMY_HASH, ISSUER, check_password_policy, hash_api_token, hash_password, new_api_token,
    normalize_email, sign_jwt, verify_jwt, verify_password,
)


class AuthError(Exception):
    """``status`` is the HTTP status the API should answer with."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def resolve_secret(settings: Settings) -> str:
    """The HMAC key shared by every component. Production must set CG_AUTH_SECRET; in dev a random key is generated once and
    kept in a 0600 file so sessions survive restarts."""
    if settings.auth_secret:
        if len(settings.auth_secret) < 16:
            raise RuntimeError("CG_AUTH_SECRET must be at least 16 characters")
        return settings.auth_secret
    if settings.env != "dev":
        raise RuntimeError("CG_AUTH_SECRET must be set when CG_ENV is not 'dev'")
    f = Path(settings.data_dir) / ".cg_auth_secret"
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.exists():
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(48))
    return f.read_text().strip()


class RateLimiter:
    """Sliding window per key (IP). In memory: fine for one process; put it behind your proxy for more."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                raise AuthError("too many attempts; wait a minute and try again", 429)
            q.append(now)


class AuthService:
    def __init__(self, repo: SqlAuthRepo, settings: Settings, secret: str):
        self.repo, self.settings, self._secret = repo, settings, secret
        self.limiter = RateLimiter(20, 60.0)

    @property
    def secret(self) -> str:
        return self._secret

    # ---- sessions
    def issue(self, user: User) -> tuple[str, datetime]:
        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=self.settings.token_ttl_s)
        claims = {"iss": ISSUER, "sub": user.id, "name": user.name, "email": user.email, "role": user.role,
                  "ver": user.token_version, "iat": int(now.timestamp()), "exp": int(exp.timestamp()), "jti": secrets.token_hex(8)}
        return sign_jwt(claims, self._secret), exp

    def register(self, email: str, name: str, password: str, ip: str = "") -> tuple[User, str, datetime]:
        self.limiter.check("register:" + ip)
        first = self.repo.count_users() == 0
        if not first and not self.settings.allow_registration:
            raise AuthError("registration is closed; ask an administrator for an account", 403)
        try:
            email = normalize_email(email)
            check_password_policy(password, email)
        except ValueError as e:
            raise AuthError(str(e), 422) from e
        name = (name or "").strip()[:128] or email.split("@")[0]
        if self.repo.get_by_email(email) is not None:
            raise AuthError("an account with this email already exists", 409)
        user = User(email=email, name=name, role="admin" if first else "researcher")  # the first account administers the instance
        self.repo.add_user(user, hash_password(password))
        token, exp = self.issue(user)
        return user, token, exp

    def login(self, email: str, password: str, ip: str = "") -> tuple[User, str, datetime]:
        self.limiter.check("login:" + ip)
        try:
            email = normalize_email(email)
        except ValueError:
            email = ""
        found = self.repo.get_by_email(email) if email else None
        now = utcnow()
        if found is None:
            verify_password(password, DUMMY_HASH)  # equalise timing
            raise AuthError("invalid email or password")
        user, pw_hash, failed, locked_until = found
        if locked_until and locked_until > now:
            raise AuthError("this account is temporarily locked after repeated failures; try again later", 429)
        if not verify_password(password, pw_hash):
            failed += 1
            lock = now + timedelta(seconds=self.settings.lockout_s) if failed >= self.settings.login_max_failures else None
            self.repo.record_login_failure(user.id, 0 if lock else failed, lock)
            raise AuthError("invalid email or password")
        if user.disabled:
            raise AuthError("this account is disabled", 403)
        self.repo.record_login_success(user.id, now)
        token, exp = self.issue(user)
        return user, token, exp

    def authenticate(self, token: str) -> User | None:
        """User for a session JWT or an API token; None if invalid, expired, revoked or the user is disabled."""
        if token.startswith(API_TOKEN_PREFIX):
            t = self.repo.token_by_hash(hash_api_token(token))
            if t is None or t.revoked or (t.expires_at and t.expires_at <= utcnow()):
                return None
            user = self.repo.get_user(t.user_id)
            if user is None or user.disabled:
                return None
            self.repo.touch_token(t.id, utcnow())
            return user
        claims = verify_jwt(token, self._secret)
        if claims is None:
            return None
        user = self.repo.get_user(str(claims.get("sub")))
        if user is None or user.disabled or int(claims.get("ver", -1)) != user.token_version:
            return None
        return user

    # ---- account management
    def change_password(self, user: User, current: str, new: str) -> tuple[str, datetime]:
        found = self.repo.get_by_email(user.email)
        if found is None or not verify_password(current, found[1]):
            raise AuthError("current password is incorrect", 403)
        try:
            check_password_policy(new, user.email)
        except ValueError as e:
            raise AuthError(str(e), 422) from e
        updated = self.repo.update_user(user.id, password_hash=hash_password(new), bump_token_version=True)
        assert updated is not None
        return self.issue(updated)  # every older session token is now invalid

    def logout_all(self, user: User) -> None:
        self.repo.update_user(user.id, bump_token_version=True)

    def create_api_token(self, user: User, name: str, expires_days: int | None = None) -> tuple[ApiToken, str]:
        name = (name or "").strip()[:128]
        if not name:
            raise AuthError("give the token a name", 422)
        plain, digest = new_api_token()
        tok = ApiToken(user_id=user.id, name=name, prefix=plain[:8],
                       expires_at=utcnow() + timedelta(days=expires_days) if expires_days else None)
        self.repo.add_token(tok, digest)
        return tok, plain

    def update_user(self, admin: User, target_id: str, *, role: str | None, disabled: bool | None) -> User:
        if role is not None and role not in ROLES:
            raise AuthError(f"role must be one of {ROLES}", 422)
        target = self.repo.get_user(target_id)
        if target is None:
            raise AuthError("no such user", 404)
        removing_admin = target.role == "admin" and ((role is not None and role != "admin") or disabled is True)
        if removing_admin and self.repo.count_admins() <= 1:
            raise AuthError("cannot demote or disable the last administrator", 409)
        updated = self.repo.update_user(target_id, role=role, disabled=disabled, bump_token_version=bool(role or disabled))
        assert updated is not None
        return updated
