import json
import logging
import re
from pathlib import Path

from crisisgrid_api.config import Settings
from crisisgrid_api.logging import JsonFormatter
from crisisgrid_api.telemetry import span


def test_settings_defaults_and_env_override():
    assert Settings.from_env({}).database_url.startswith("sqlite")
    s = Settings.from_env({"CG_DATABASE_URL": "postgresql+psycopg://x/y", "CG_LOG_LEVEL": "debug"})
    assert (s.database_url, s.log_level) == ("postgresql+psycopg://x/y", "DEBUG")


def test_json_log_format_includes_extras():
    rec = logging.LogRecord("crisisgrid.t", logging.INFO, __file__, 1, "hello %s", ("w",), None)
    rec.run_id = "r1"
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "hello w" and out["level"] == "INFO" and out["run_id"] == "r1"


def test_span_is_a_noop_context_manager():
    with span("x", a=1):
        pass


def test_only_db_package_imports_sqlalchemy():
    pkg = Path(__file__).resolve().parents[1] / "crisisgrid_api"
    bad = re.compile(r"^\s*(from|import)\s+(sqlalchemy|alembic)\b", re.M)
    offenders = [
        f.relative_to(pkg).as_posix()
        for f in pkg.rglob("*.py")
        if not f.relative_to(pkg).as_posix().startswith("db/") and bad.search(f.read_text(encoding="utf-8"))
    ]
    # main.py only uses Engine/text for the health probe; keep that the single exception
    assert offenders == ["main.py"]
