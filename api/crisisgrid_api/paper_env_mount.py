"""Mounts the Paper Environment API inside this app (same process, same login).

paper_env is imported lazily and optionally: if the package is not installed the rest of CrisisGrid still works and
``/paper-env`` simply is not there (the health endpoint says so).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .config import Settings
from .container import AppContainer

log = logging.getLogger("crisisgrid.paper_env")


def mount_paper_env(app: FastAPI, container: AppContainer, settings: Settings) -> Any | None:
    """Returns the paper_env ``App`` (to start/stop with this app's lifespan), or None when it is not mounted."""
    if not settings.paper_env_enabled:
        return None
    try:
        from paper_env.api import create_api
        from paper_env.auth import Principal
        from paper_env.config import Settings as PESettings
        from paper_env.container import build_app
    except ImportError as e:  # pragma: no cover - depends on the install
        log.warning("paper_env is not installed; /paper-env is disabled (%s)", e)
        return None

    class JwtAuthenticator:
        """Same users, same tokens: session JWTs and API tokens verified by the CrisisGrid auth service."""

        def authenticate(self, token: str) -> Principal | None:
            user = container.auth.authenticate(token)
            return Principal(user.id, user.name, user.role) if user else None

    pe_settings = PESettings(data_dir=Path(settings.paper_env_dir).resolve(), enable_terminal=settings.paper_env_enable_terminal,
                             require_auth=settings.auth_enabled)
    core = build_app(pe_settings, authenticator=JwtAuthenticator() if settings.auth_enabled else None)
    app.mount("/paper-env", create_api(core, manage_lifecycle=False))
    return core
