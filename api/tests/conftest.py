import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

API_ROOT = Path(__file__).resolve().parents[1]

# The pre-existing suites exercise the pipeline without logging in and without mounting paper_env.
# test_auth.py turns authentication back on explicitly.
os.environ.setdefault("CG_AUTH_ENABLED", "0")
os.environ.setdefault("CG_PAPER_ENV", "0")


def alembic_config(url: str) -> Config:
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "crisisgrid_api" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def migrated_url(db_url):
    command.upgrade(alembic_config(db_url), "head")
    return db_url
