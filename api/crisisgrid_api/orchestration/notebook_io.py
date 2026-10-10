"""Notebook import/export (.ipynb, nbformat 4) for the pipeline. Cells are plain dicts {"kind": "code"|"markdown", "source": str}.
Import only reads the JSON: nothing in the notebook is executed here."""
from __future__ import annotations

from typing import Any

MAX_CELLS = 500
MAX_CELL_CHARS = 200_000


class NotebookError(ValueError):
    pass


def _source(raw: Any) -> str:
    if isinstance(raw, list):
        return "".join(str(x) for x in raw)
    if isinstance(raw, str):
        return raw
    raise NotebookError("cell source must be a string or a list of strings")


def parse_notebook(obj: Any) -> list[dict[str, str]]:
    """Accepts a full .ipynb document or a bare {"cells": [{"kind"|"cell_type": ..., "source": ...}]}."""
    if not isinstance(obj, dict) or not isinstance(obj.get("cells"), list):
        raise NotebookError("expected an object with a 'cells' list")
    cells: list[dict[str, str]] = []
    for i, c in enumerate(obj["cells"]):
        if not isinstance(c, dict):
            raise NotebookError(f"cell {i} is not an object")
        kind = c.get("cell_type", c.get("kind", "code"))
        if kind not in ("code", "markdown", "raw"):
            raise NotebookError(f"cell {i} has unknown type {kind!r}")
        src = _source(c.get("source", ""))
        if len(src) > MAX_CELL_CHARS:
            raise NotebookError(f"cell {i} is too large")
        cells.append({"kind": "markdown" if kind != "code" else "code", "source": src})
    if len(cells) > MAX_CELLS:
        raise NotebookError(f"too many cells (max {MAX_CELLS})")
    if not any(c["kind"] == "code" and c["source"].strip() for c in cells):
        raise NotebookError("the notebook has no code to run")
    return cells


def to_ipynb(cells: list[dict[str, str]]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for c in cells:
        src = c["source"].splitlines(keepends=True)
        if c["kind"] == "code":
            out.append({"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": src})
        else:
            out.append({"cell_type": "markdown", "metadata": {}, "source": src})
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": {"language_info": {"name": "python"}}, "cells": out}
