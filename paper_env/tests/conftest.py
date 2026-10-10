import pytest

from paper_env.repos.memory import memory_uow_factory
from paper_env.repos.sqlite import sqlite_uow_factory


@pytest.fixture(params=["memory", "sqlite"])
def uow_factory(request, tmp_path):
    if request.param == "memory":
        return memory_uow_factory()
    return sqlite_uow_factory(f"sqlite:///{tmp_path / 'test.db'}")
