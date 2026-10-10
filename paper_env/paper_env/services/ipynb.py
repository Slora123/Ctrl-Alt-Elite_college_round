"""Jupyter notebook (.ipynb, nbformat 4) import and export.

Import never executes anything. Package installs written inside cells (``!pip install x`` / ``%pip install x``)
are NOT kept in the cell: they are rewritten to a comment and reported as packages, because environment changes
go through the Env manager (and the pre-execute hook), never through a cell.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

from ..domain import Author
from ..env import EnvManager
from ..env.manager import InvalidSpec, validate_requirement
from ..events import EventBus
from ..repos import UnitOfWorkFactory
from .notebook import NotebookService

MAX_CELLS = 2000
MAX_SOURCE_BYTES = 2_000_000
_PIP = re.compile(r"^(\s*)[%!]\s*(?:python\s+-m\s+)?pip3?\s+install\s+(.*)$")
_FLAGS_WITH_ARG = {"-r", "--requirement", "-c", "--constraint", "-i", "--index-url", "--extra-index-url", "-f",
                   "--find-links", "-e", "--editable", "--target", "-t", "--prefix", "--root"}


class NotebookFormatError(ValueError):
    pass


@dataclass
class ImportReport:
    cells_added: int = 0
    markdown_cells: int = 0
    rewritten_pip_lines: list[dict[str, Any]] = field(default_factory=list)  # {cell_index, original}
    packages: list[str] = field(default_factory=list)
    unsupported_pip_args: list[str] = field(default_factory=list)
    skipped_magics: list[dict[str, Any]] = field(default_factory=list)  # other %/! lines left as-is (they will fail in a clean run)
    installs: list[dict[str, Any]] = field(default_factory=list)  # results when install_packages=True
    warnings: list[str] = field(default_factory=list)


def parse_pip_args(rest: str) -> tuple[list[str], list[str]]:
    """(requirements, unsupported_args) from the text after ``pip install``. Flags that change where packages come
    from (index URLs, -r files, editable paths) are reported, never followed."""
    reqs: list[str] = []
    bad: list[str] = []
    try:
        toks = shlex.split(rest, comments=True)
    except ValueError:
        return [], [rest.strip()]
    it = iter(toks)
    for t in it:
        if t in _FLAGS_WITH_ARG:
            arg = next(it, "")
            bad.append(f"{t} {arg}".strip())
        elif t.startswith("-"):
            continue  # harmless flags: -q, -U, --upgrade, --no-cache-dir ...
        else:
            try:
                reqs.append(validate_requirement(t))
            except InvalidSpec:
                bad.append(t)
    return reqs, bad


def _src(cell: dict[str, Any]) -> str:
    s = cell.get("source", "")
    return "".join(s) if isinstance(s, list) else str(s)


class IpynbService:
    def __init__(self, uow_factory: UnitOfWorkFactory, bus: EventBus, notebook: NotebookService, envs: EnvManager):
        self._uow, self.bus, self.notebook, self.envs = uow_factory, bus, notebook, envs

    # ------------------------------------------------------------------ import
    def import_notebook(self, paper_env_id: str, nb: dict[str, Any], *, install_packages: bool = False,
                        author: Author = Author.HUMAN) -> ImportReport:
        cells = nb.get("cells") if isinstance(nb, dict) else None
        if not isinstance(cells, list):
            raise NotebookFormatError("not a notebook: missing a 'cells' list")
        if len(cells) > MAX_CELLS:
            raise NotebookFormatError(f"too many cells ({len(cells)} > {MAX_CELLS})")
        if int(nb.get("nbformat", 4)) < 4:
            raise NotebookFormatError("only nbformat 4 notebooks are supported")
        rep = ImportReport()
        for i, c in enumerate(cells):
            if not isinstance(c, dict):
                raise NotebookFormatError(f"cell {i} is not an object")
            src = _src(c)
            if len(src.encode()) > MAX_SOURCE_BYTES:
                raise NotebookFormatError(f"cell {i} is too large")
            kind = c.get("cell_type", "code")
            meta = (c.get("metadata") or {}).get("paper_env") or {}
            if kind in ("markdown", "raw"):
                self.notebook.add_cell(paper_env_id, src, author, cell_type="markdown")
                rep.markdown_cells += 1
                continue
            if kind != "code":
                rep.warnings.append(f"cell {i}: unknown cell_type {kind!r} skipped")
                continue
            lines = []
            for ln in src.splitlines():
                m = _PIP.match(ln)
                if m:
                    reqs, bad = parse_pip_args(m.group(2))
                    rep.packages.extend(r for r in reqs if r not in rep.packages)
                    rep.unsupported_pip_args.extend(bad)
                    rep.rewritten_pip_lines.append({"cell_index": i, "original": ln.strip()})
                    lines.append(f"{m.group(1)}# [removed on import] {ln.strip()}  -> use the Env panel (install_package)")
                else:
                    if re.match(r"^\s*[%!]", ln) and not ln.lstrip().startswith(("%matplotlib", "%load_ext", "%autoreload", "%%time")):
                        rep.skipped_magics.append({"cell_index": i, "line": ln.strip()[:120]})
                    lines.append(ln)
            new_src = "\n".join(lines) + ("\n" if src.endswith("\n") else "")
            self.notebook.add_cell(
                paper_env_id, new_src, author, declared_inputs=list(meta.get("declared_inputs") or []),
                declared_outputs=list(meta.get("declared_outputs") or []))
            rep.cells_added += 1
        if rep.skipped_magics:
            rep.warnings.append("some IPython magics were kept; a clean run does not support them and the cell will fail")
        if install_packages:
            for pkg in rep.packages:
                try:
                    req = Requirement(pkg)
                except InvalidRequirement:
                    rep.installs.append({"package": pkg, "status": "invalid", "error": "not a plain requirement"})
                    continue
                pins = [x.version for x in req.specifier if x.operator == "=="]
                if len(req.specifier) > (1 if pins else 0) or (pins and len(req.specifier) != 1):
                    rep.warnings.append(f"{pkg}: only a single '==' pin is applied; other constraints were dropped")
                r = self.envs.install_package(paper_env_id, req.name, pins[0] if pins else None)
                rep.installs.append({"package": pkg, "status": r.status, "error": r.error,
                                     "blocked_action_id": r.blocked_action_id})
        return rep

    # ------------------------------------------------------------------ export
    def export_notebook(self, paper_env_id: str, *, include_outputs: bool = True) -> dict[str, Any]:
        with self._uow() as u:
            env = u.paper_envs.get(paper_env_id)
            spec = u.paper_envs.get_spec(env.env_spec_id)
            cells = u.cells.list_cells(paper_env_id)
            sources = {c.id: u.cells.get_version(c.current_version_id).source for c in cells}
            latest = {}
            if include_outputs:
                runs = u.runs.list_runs(paper_env_id)
                if runs:
                    latest = {cr.cell_id: cr for cr in u.runs.list_cell_runs(runs[-1].id)}
        out_cells = []
        for n, c in enumerate(cells, 1):
            lines = sources[c.id].splitlines(keepends=True)
            if c.cell_type == "markdown":
                out_cells.append({"cell_type": "markdown", "metadata": {}, "source": lines})
                continue
            cr = latest.get(c.id)
            outputs: list[dict[str, Any]] = []
            if cr is not None:
                for o in cr.outputs:
                    if o.get("type") == "stream":
                        outputs.append({"output_type": "stream", "name": o["name"], "text": o["text"]})
                    elif o.get("type") in ("execute_result", "display_data"):
                        outputs.append({"output_type": o["type"], "data": o.get("data", {}), "metadata": {},
                                        **({"execution_count": n} if o["type"] == "execute_result" else {})})
                if cr.error is not None:
                    outputs.append({"output_type": "error", "ename": cr.error.type, "evalue": cr.error.message,
                                    "traceback": cr.error.traceback.splitlines()})
            out_cells.append({
                "cell_type": "code", "execution_count": n if cr is not None and cr.status.value == "success" else None,
                "metadata": {"paper_env": {"cell_id": c.id, "declared_inputs": c.declared_inputs,
                                           "declared_outputs": c.declared_outputs, "status": c.status.value}},
                "outputs": outputs, "source": lines})
        return {
            "nbformat": 4, "nbformat_minor": 5,
            "metadata": {
                "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
                "language_info": {"name": "python", "version": spec.python_version},
                "paper_env": {"alias": env.alias, "paper_hash": env.paper_hash, "seed": spec.seed,
                              "lockfile_hash": spec.lockfile_hash, "notebook_version": env.current_notebook_version,
                              "requirements": spec.requirements, "pins": spec.pins},
            },
            "cells": out_cells,
        }
