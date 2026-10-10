"""Boundary rules: persistence libs are confined to repos/sqlite; domain stays plain."""
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "paper_env"
FORBIDDEN = re.compile(r"^\s*(from|import)\s+(sqlalchemy|sqlmodel|alembic)\b", re.M)


def test_only_sqlite_package_imports_orm():
    offenders = []
    for f in PKG.rglob("*.py"):
        rel = f.relative_to(PKG).as_posix()
        if rel.startswith("repos/sqlite/"):
            continue
        if FORBIDDEN.search(f.read_text()):
            offenders.append(rel)
    assert offenders == []
