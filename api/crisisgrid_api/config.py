from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    env: str = "dev"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./crisisgrid.db"
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "crisisgrid"
    llm_chain: str = ""  # comma-separated LiteLLM model ids, tried in order, e.g. "groq/qwen/qwen3-32b,ollama/qwen2.5:7b"
    llm_api_key: str = ""  # applied to EVERY model in the chain; for mixed providers leave it empty and use each provider's own env var
    llm_api_base: str = ""  # e.g. a self-hosted NVIDIA NIM / vLLM / Ollama endpoint (OpenAI-compatible, usually ends in /v1)
    github_token: str = ""
    reaper_interval_s: float = 30.0  # how often to reap jobs whose worker stopped heartbeating; 0 disables
    worker_registration_token: str = ""  # if empty, registration is only open when CG_ENV=dev
    # --- user authentication
    auth_enabled: bool = field(default_factory=lambda: os.environ.get("CG_AUTH_ENABLED", "1") not in ("0", "false", "no"))  # CG_AUTH_ENABLED=0 runs everything as a built-in admin (single-user local use / tests)
    auth_secret: str = ""  # HMAC key shared with the website backend and paper_env; required unless CG_ENV=dev
    token_ttl_s: int = 12 * 3600
    allow_registration: bool = True  # the very first account is always allowed (and becomes admin)
    login_max_failures: int = 5
    lockout_s: int = 900
    data_dir: str = "."  # where generated local state lives (auth secret, ...)
    # --- paper environment (mounted in-process under /paper-env)
    paper_env_enabled: bool = field(default_factory=lambda: os.environ.get("CG_PAPER_ENV", "1") not in ("0", "false", "no"))
    paper_env_dir: str = "./paper_env_data"
    paper_env_enable_terminal: bool = False

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
            llm_chain=e.get("CG_LLM_CHAIN", d.llm_chain),
            llm_api_key=e.get("CG_LLM_API_KEY", d.llm_api_key),
            llm_api_base=e.get("CG_LLM_API_BASE", d.llm_api_base),
            github_token=e.get("CG_GITHUB_TOKEN", d.github_token),
            reaper_interval_s=float(e.get("CG_REAPER_INTERVAL_S", d.reaper_interval_s)),
            worker_registration_token=e.get("CG_WORKER_REGISTRATION_TOKEN", d.worker_registration_token),
            auth_enabled=e.get("CG_AUTH_ENABLED", "1") not in ("0", "false", "no"),
            auth_secret=e.get("CG_AUTH_SECRET", d.auth_secret),
            token_ttl_s=int(e.get("CG_TOKEN_TTL_S", d.token_ttl_s)),
            allow_registration=e.get("CG_ALLOW_REGISTRATION", "1") not in ("0", "false", "no"),
            login_max_failures=int(e.get("CG_LOGIN_MAX_FAILURES", d.login_max_failures)),
            lockout_s=int(e.get("CG_LOCKOUT_S", d.lockout_s)),
            data_dir=e.get("CG_DATA_DIR", d.data_dir),
            paper_env_enabled=e.get("CG_PAPER_ENV", "1") not in ("0", "false", "no"),
            paper_env_dir=e.get("CG_PAPER_ENV_DIR", d.paper_env_dir),
            paper_env_enable_terminal=e.get("CG_PAPER_ENV_TERMINAL", "0") in ("1", "true", "yes"),
        )
