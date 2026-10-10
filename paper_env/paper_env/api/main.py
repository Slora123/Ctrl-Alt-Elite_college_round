from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from ..config import Settings
from ..container import App, build_app
from .deps import install_error_handlers
from .routes import router


def create_api(core: App | None = None) -> FastAPI:
    """FastAPI app around the composition root. Pass a prebuilt ``App`` to inject a hook / backend / secrets."""
    core = core or build_app(Settings(data_dir=Path(os.environ.get("PAPER_ENV_DATA_DIR", "paper_env_data")).resolve()))

    @asynccontextmanager
    async def lifespan(api: FastAPI):
        api.state.startup_report = core.startup()
        yield
        core.shutdown()

    api = FastAPI(title="CrisisGrid Paper Environment", version="0.1.0", lifespan=lifespan)
    api.state.core = core
    install_error_handlers(api)
    api.include_router(router)
    return api


def app_factory() -> FastAPI:  # uvicorn --factory paper_env.api.main:app_factory
    return create_api()
