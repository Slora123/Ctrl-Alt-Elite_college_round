from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    env: str = "dev"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./crisisgrid.db"
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "crisisgrid"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        e = os.environ if environ is None else environ
        d = cls()
        return cls(
            env=e.get("CG_ENV", d.env),
            log_level=e.get("CG_LOG_LEVEL", d.log_level).upper(),
            database_url=e.get("CG_DATABASE_URL", d.database_url),
            redis_url=e.get("CG_REDIS_URL", d.redis_url),
            s3_endpoint=e.get("CG_S3_ENDPOINT", d.s3_endpoint),
            s3_bucket=e.get("CG_S3_BUCKET", d.s3_bucket),
        )
