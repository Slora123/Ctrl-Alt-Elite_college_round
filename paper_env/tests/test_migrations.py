from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from paper_env.repos.sqlite.db import make_engine, migrate
from paper_env.repos.sqlite.tables import Base


def test_migrations_match_orm_metadata(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    migrate(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_in_memory_migrate_works():
    engine = make_engine("sqlite://")
    migrate(engine)
    with engine.connect() as conn:
        names = set(MigrationContext.configure(conn).connection.dialect.get_table_names(conn))
    assert {"paper_envs", "cells", "cell_versions", "runs", "cell_runs", "datasets", "events"} <= names
