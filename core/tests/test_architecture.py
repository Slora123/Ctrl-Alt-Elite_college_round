"""core is pure: no persistence, web, or LLM imports."""
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "crisisgrid_core"
FORBIDDEN = re.compile(r"^\s*(from|import)\s+(sqlalchemy|alembic|fastapi|redis|litellm|pydantic_ai|paper_env)\b", re.M)


def test_core_has_no_infrastructure_imports():
    offenders = [f.name for f in PKG.rglob("*.py") if FORBIDDEN.search(f.read_text(encoding="utf-8"))]
    assert offenders == []
