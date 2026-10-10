"""Notebook: cells, immutable version history, the single edit door (``apply_edit``) and stale propagation."""
from __future__ import annotations

import difflib

import re

from ..domain import (
    Author, Cell, CellStatus, CellType, CellVersion, EventType, content_hash,
)
from ..events import EventBus
from ..kernel.static import analyze_cell
from ..repos import NotFound, UnitOfWork, UnitOfWorkFactory


class CellBusy(RuntimeError):
    pass


_RAN = (CellStatus.SUCCESS, CellStatus.FAILED, CellStatus.SKIPPED, CellStatus.STALE, CellStatus.BLOCKED)


def make_diff(old: str, new: str, label: str = "cell") -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        f"{label} (previous)", f"{label} (new)"))


class NotebookService:
    def __init__(self, uow_factory: UnitOfWorkFactory, bus: EventBus):
        self._uow, self.bus = uow_factory, bus

    # ------------------------------------------------------------------ cells
    def add_cell(self, paper_env_id: str, source: str, author: Author = Author.HUMAN, *,
                 position: int | None = None, declared_inputs: list[str] | None = None,
                 declared_outputs: list[str] | None = None, node_id: str | None = None,
                 source_span: dict | None = None, cell_type: CellType = "code") -> Cell:
        with self._uow() as u:
            env = u.paper_envs.get(paper_env_id)
            cells = u.cells.list_cells(paper_env_id)
            pos = len(cells) if position is None else max(0, min(position, len(cells)))
            for c in cells[pos:]:  # make room; later cells now sit downstream of the new one
                c.position += 1
                if c.status in _RAN:
                    c.status = CellStatus.STALE
                u.cells.update_cell(c)
            env.current_notebook_version += 1
            u.paper_envs.update(env)
            is_code = cell_type == "code"
            cell = Cell(paper_env_id=paper_env_id, position=pos, node_id=node_id, source_span=source_span,
                        cell_type=cell_type, declared_inputs=(declared_inputs or []) if is_code else [],
                        declared_outputs=(declared_outputs or []) if is_code else [])
            cell.input_warnings = analyze_cell(source, cell.declared_inputs) if is_code else []
            ver = CellVersion(cell_id=cell.id, source=source, author=author, content_hash=content_hash(source),
                              notebook_version=env.current_notebook_version)
            cell.current_version_id = ver.id
            u.cells.add_cell(cell)
            u.cells.add_version(ver)
            ev = self.bus.append(u, EventType.CELL_EDITED, {
                "change": "created", "cell_id": cell.id, "cell_version_id": ver.id, "author": author.value,
                "position": pos, "cell_type": cell_type, "notebook_version": env.current_notebook_version}, paper_env_id=paper_env_id)
        self.bus.dispatch(ev)
        return cell

    def get_cell(self, cell_id: str) -> Cell:
        with self._uow() as u:
            return u.cells.get_cell(cell_id)

    def list_cells(self, paper_env_id: str) -> list[Cell]:
        with self._uow() as u:
            return u.cells.list_cells(paper_env_id)

    def source_of(self, cell_id: str) -> str:
        with self._uow() as u:
            c = u.cells.get_cell(cell_id)
            return u.cells.get_version(c.current_version_id).source

    def versions(self, cell_id: str) -> list[CellVersion]:
        with self._uow() as u:
            u.cells.get_cell(cell_id)
            return u.cells.list_versions(cell_id)

    def update_declarations(self, cell_id: str, *, declared_inputs: list[str] | None = None,
                            declared_outputs: list[str] | None = None) -> Cell:
        """Metadata only; the source is untouched (and therefore needs no new version)."""
        with self._uow() as u:
            c = u.cells.get_cell(cell_id)
            if declared_inputs is not None:
                c.declared_inputs = declared_inputs
            if declared_outputs is not None:
                c.declared_outputs = declared_outputs
            if c.cell_type != "code":
                raise ValueError("markdown cells have no declared inputs or outputs")
            c.input_warnings = analyze_cell(u.cells.get_version(c.current_version_id).source, c.declared_inputs)
            u.cells.update_cell(c)
            return c

    def delete_cell(self, cell_id: str) -> None:
        with self._uow() as u:
            c = u.cells.get_cell(cell_id)
            if c.status == CellStatus.RUNNING:
                raise CellBusy(f"cell {cell_id} is running")
            env = u.paper_envs.get(c.paper_env_id)
            u.cells.delete_cell(cell_id)
            for o in u.cells.list_cells(c.paper_env_id):
                if o.position > c.position:
                    o.position -= 1
                    if o.status in _RAN:
                        o.status = CellStatus.STALE
                    u.cells.update_cell(o)
            env.current_notebook_version += 1
            u.paper_envs.update(env)
            ev = self.bus.append(u, EventType.CELL_EDITED, {
                "change": "deleted", "cell_id": cell_id, "notebook_version": env.current_notebook_version},
                paper_env_id=c.paper_env_id)
        self.bus.dispatch(ev)

    # ------------------------------------------------------------------ the single edit door
    def apply_edit(self, cell_id: str, new_source: str, author: Author, *, _reverted_from: str | None = None) -> CellVersion:
        """The ONLY way a cell's source changes. Appends a CellVersion (never mutates one), stores a
        diff against the previous version, marks this cell and everything downstream ``stale``, and
        emits ``cell_edited``. Re-applying identical source is a no-op and returns the current version."""
        with self._uow() as u:
            cell = u.cells.get_cell(cell_id)
            if cell.status == CellStatus.RUNNING:
                raise CellBusy(f"cell {cell_id} is running; interrupt it before editing")
            prev = u.cells.get_version(cell.current_version_id)
            if content_hash(new_source) == prev.content_hash:
                return prev
            env = u.paper_envs.get(cell.paper_env_id)
            env.current_notebook_version += 1
            u.paper_envs.update(env)
            ver = CellVersion(cell_id=cell_id, source=new_source, author=author, parent_version_id=prev.id,
                              content_hash=content_hash(new_source), diff=make_diff(prev.source, new_source),
                              notebook_version=env.current_notebook_version)
            u.cells.add_version(ver)
            cell.current_version_id = ver.id
            cell.input_warnings = analyze_cell(new_source, cell.declared_inputs) if cell.cell_type == "code" else []
            u.cells.update_cell(cell)
            # editing documentation (markdown) changes no results, so nothing goes stale
            stale = self._mark_stale(u, cell.paper_env_id, cell.position, include_self=True) if cell.cell_type == "code" else []
            ev = self.bus.append(u, EventType.CELL_EDITED, {
                "change": "reverted" if _reverted_from else "edited", "cell_id": cell_id, "cell_version_id": ver.id,
                "parent_version_id": prev.id, "author": author.value, "diff": ver.diff,
                "reverted_to": _reverted_from, "stale_cells": stale, "notebook_version": env.current_notebook_version},
                paper_env_id=cell.paper_env_id)
        self.bus.dispatch(ev)
        return ver

    def revert_to(self, cell_version_id: str, author: Author = Author.HUMAN) -> CellVersion:
        """Re-apply an old version's source as a NEW version (history is append-only)."""
        with self._uow() as u:
            target = u.cells.get_version(cell_version_id)
        return self.apply_edit(target.cell_id, target.source, author, _reverted_from=cell_version_id)

    # ------------------------------------------------------------------ find & replace
    def find(self, paper_env_id: str, pattern: str, *, regex: bool = False, case: bool = False,
             limit: int = 500) -> list[dict]:
        """Matches across all cells (code and markdown): [{cell_id, position, line, column, text}]."""
        rx = self._compile(pattern, regex, case)
        out = []
        with self._uow() as u:
            for c in u.cells.list_cells(paper_env_id):
                src = u.cells.get_version(c.current_version_id).source
                for i, line in enumerate(src.splitlines()):
                    for m in rx.finditer(line):
                        out.append({"cell_id": c.id, "position": c.position, "line": i + 1, "column": m.start(),
                                    "text": line.strip()[:200]})
                        if len(out) >= limit:
                            return out
        return out

    def replace_all(self, paper_env_id: str, pattern: str, replacement: str, author: Author, *, regex: bool = False,
                    case: bool = False, cell_ids: list[str] | None = None) -> list[dict]:
        """Replace in every matching cell through ``apply_edit`` (so each change is a versioned edit with an author)."""
        rx = self._compile(pattern, regex, case)
        changed = []
        for c in self.list_cells(paper_env_id):
            if cell_ids is not None and c.id not in cell_ids:
                continue
            src = self.source_of(c.id)
            new, n = rx.subn(replacement if regex else replacement.replace("\\", "\\\\"), src)
            if n and new != src:
                ver = self.apply_edit(c.id, new, author)
                changed.append({"cell_id": c.id, "replacements": n, "cell_version_id": ver.id})
        return changed

    @staticmethod
    def _compile(pattern: str, regex: bool, case: bool) -> "re.Pattern[str]":
        if not pattern:
            raise ValueError("search pattern is empty")
        try:
            return re.compile(pattern if regex else re.escape(pattern), 0 if case else re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"invalid regular expression: {e}") from e

    # ------------------------------------------------------------------ staleness
    def _mark_stale(self, u: UnitOfWork, paper_env_id: str, from_position: int, include_self: bool) -> list[str]:
        """Downstream == later position (kernel state flows top to bottom). Cells that never ran stay pending."""
        out = []
        for c in u.cells.list_cells(paper_env_id):
            if (c.position > from_position or (include_self and c.position == from_position)) and c.status in _RAN:
                if c.status != CellStatus.STALE:
                    c.status = CellStatus.STALE
                    u.cells.update_cell(c)
                out.append(c.id)
        return out

    def mark_downstream_stale(self, cell_id: str, include_self: bool = False) -> list[str]:
        with self._uow() as u:
            c = u.cells.get_cell(cell_id)
            return self._mark_stale(u, c.paper_env_id, c.position, include_self)

    def mark_all_stale(self, paper_env_id: str, except_ids: set[str] = frozenset()) -> list[str]:
        """Used when kernel state is lost (restart / death): nothing that ran can claim its state exists."""
        out = []
        with self._uow() as u:
            for c in u.cells.list_cells(paper_env_id):
                if c.status in _RAN and c.id not in except_ids and c.status != CellStatus.STALE:
                    c.status = CellStatus.STALE
                    u.cells.update_cell(c)
                    out.append(c.id)
        return out
