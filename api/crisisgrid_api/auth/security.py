"""Auth primitives using only the standard library: scrypt password hashing, HS256 JWTs, API token hashing.

The JWT format is deliberately plain HS256 so the Node website backend and the paper_env module can verify the same
tokens with a few lines of HMAC code and the shared ``CG_AUTH_SECRET``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any

ISSUER = "crisisgrid"
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
MIN_PASSWORD = 10


class PasswordPolicyError(ValueError):
    pass


def normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not _EMAIL.match(e) or len(e) > 320:
        raise ValueError("invalid email address")
    return e


def check_password_policy(password: str, email: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise PasswordPolicyError(f"password must be at least {MIN_PASSWORD} characters")
    if len(password) > 256:
        raise PasswordPolicyError("password is too long")
    if password.lower() in {"password123", "1234567890", "qwertyuiop"} or (email and password.lower() == email.lower()):
        raise PasswordPolicyError("password is too easy to guess")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


# a real hash to compare against when the email is unknown, so timing does not reveal which emails exist
DUMMY_HASH = hash_password("not-a-real-password")

# ------------------------------------------------------------------ JWT (HS256)
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_jwt(claims: dict[str, Any], secret: str) -> str:
    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64u(json.dumps(claims, separators=(",", ":")).encode())
    sig = _b64u(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


def verify_jwt(token: str, secret: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Claims if the signature, issuer and expiry are valid, else None. Rejects any alg other than HS256."""
    try:
        h, b, s = token.split(".")
        if json.loads(_b64u_dec(h)).get("alg") != "HS256":
            return None
        want = _b64u(hmac.new(secret.encode(), f"{h}.{b}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(want, s):
            return None
        claims = json.loads(_b64u_dec(b))
        t = time.time() if now is None else now
        if claims.get("iss") != ISSUER or float(claims.get("exp", 0)) <= t or float(claims.get("nbf", 0)) > t + 5:
            return None
        return claims  # type: ignore[no-any-return]
    except (ValueError, TypeError, KeyError):
        return None


# ------------------------------------------------------------------ API tokens
API_TOKEN_PREFIX = "cgt_"


def new_api_token() -> tuple[str, str]:
    """(plaintext shown once, sha256 stored)."""
    plain = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    return plain, hash_api_token(plain)


def hash_api_token(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()
