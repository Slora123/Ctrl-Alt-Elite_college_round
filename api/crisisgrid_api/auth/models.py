from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from crisisgrid_core.ids import new_id  # type: ignore[import-not-found]

ROLES = ("viewer", "researcher", "admin")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(BaseModel):
    id: str = Field(default_factory=new_id)
    email: str
    name: str
    role: str = "researcher"
    disabled: bool = False
    token_version: int = 0  # bumping it revokes every session token issued before
    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: datetime | None = None

    def public(self) -> dict[str, object]:
        return {"id": self.id, "email": self.email, "name": self.name, "role": self.role, "disabled": self.disabled,
                "created_at": self.created_at.isoformat(), "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None}

    @property
    def can_write(self) -> bool:
        return self.role in ("researcher", "admin")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class ApiToken(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    name: str
    prefix: str  # first characters, so a user can tell tokens apart; the secret part is never stored
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked: bool = False
