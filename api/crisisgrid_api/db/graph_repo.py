from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from crisisgrid_core.ids import new_id

from .base import SqlRepository
from .tables import GraphVersionRow


def _row(r: GraphVersionRow) -> dict[str, Any]:
    return {"id": r.id, "paper_id": r.paper_id, "version": r.version, "parent_version": r.parent_version, "created_by": r.created_by,
            "change_note": r.change_note, "graph": r.graph, "cells": r.cells, "run_ids": r.run_ids,
            "created_at": (r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)).isoformat()}


class SqlGraphRepo(SqlRepository):
    def add(self, paper_id: str, version: int, parent: int | None, created_by: str, note: str | None, graph: dict[str, Any],
            cells: list[dict[str, Any]]) -> dict[str, Any]:
        with self._scope() as s:
            r = GraphVersionRow(id=new_id(), paper_id=paper_id, version=version, parent_version=parent, created_by=created_by,
                                change_note=note, graph=graph, cells=cells, run_ids=[], created_at=datetime.now(timezone.utc))
            s.add(r)
            s.flush()
            return _row(r)

    def list(self, paper_id: str) -> list[dict[str, Any]]:
        with self._scope() as s:
            return [_row(r) for r in s.scalars(select(GraphVersionRow).where(GraphVersionRow.paper_id == paper_id)
                                               .order_by(GraphVersionRow.version))]

    def get(self, paper_id: str, version: int) -> dict[str, Any] | None:
        with self._scope() as s:
            r = s.scalar(select(GraphVersionRow).where(GraphVersionRow.paper_id == paper_id, GraphVersionRow.version == version))
            return None if r is None else _row(r)

    def set_cells(self, paper_id: str, version: int, cells: list[dict[str, Any]], graph: dict[str, Any]) -> None:
        with self._scope() as s:
            r = s.scalar(select(GraphVersionRow).where(GraphVersionRow.paper_id == paper_id, GraphVersionRow.version == version))
            if r is not None:
                r.cells, r.graph = cells, graph

    def add_run(self, paper_id: str, version: int, run_id: str) -> None:
        with self._scope() as s:
            r = s.scalar(select(GraphVersionRow).where(GraphVersionRow.paper_id == paper_id, GraphVersionRow.version == version))
            if r is not None:
                r.run_ids = [*r.run_ids, run_id]

    def delete_for_paper(self, paper_id: str) -> None:
        with self._scope() as s:
            for r in s.scalars(select(GraphVersionRow).where(GraphVersionRow.paper_id == paper_id)):
                s.delete(r)
