from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from crisisgrid_api.config import Settings
from crisisgrid_api.db.base import make_engine
from crisisgrid_api.main import create_app

from conftest import alembic_config


def test_migration_creates_run_events_and_downgrades(db_url):
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    insp = inspect(make_engine(db_url))
    assert {"run_events", "llm_calls", "papers", "pipeline_runs", "gates", "workers", "jobs", "alembic_version"} <= set(insp.get_table_names())
    assert {"id", "run_id", "ts", "module", "kind", "payload"} == {c["name"] for c in insp.get_columns("run_events")}
    command.downgrade(cfg, "base")
    assert "run_events" not in inspect(make_engine(db_url)).get_table_names()


def test_health_reports_applied_migration(migrated_url):
    app = create_app(Settings(database_url=migrated_url))
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "migration": "0004"}


def test_health_without_migration_is_ok_but_unmigrated(db_url):
    with TestClient(create_app(Settings(database_url=db_url))) as c:
        r = c.get("/health")
    assert r.status_code == 200 and r.json()["migration"] is None


def test_health_degraded_when_db_unreachable(tmp_path):
    bad = f"sqlite:///{(tmp_path / 'no_such_dir' / 'x.db').as_posix()}"
    with TestClient(create_app(Settings(database_url=bad))) as c:
        r = c.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "degraded"
