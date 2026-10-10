from __future__ import annotations

from sqlalchemy import select

from ..orchestration.models import Gate, PaperRecord, RunState
from .base import SqlRepository
from .tables import GateRow, PaperRow, PipelineRunRow


def _paper(r: PaperRow) -> PaperRecord:
    return PaperRecord(id=r.id, paper_hash=r.paper_hash, filename=r.filename, size=r.size, created_at=r.created_at)


class SqlPaperRepo(SqlRepository):
    def add(self, record: PaperRecord, pdf: bytes) -> None:
        with self._scope() as s:
            s.add(PaperRow(**record.model_dump(), pdf=pdf))

    def get(self, paper_id: str) -> PaperRecord | None:
        with self._scope() as s:
            r = s.get(PaperRow, paper_id)
            return None if r is None else _paper(r)

    def get_pdf(self, paper_id: str) -> bytes | None:
        with self._scope() as s:
            r = s.get(PaperRow, paper_id)
            return None if r is None else bytes(r.pdf)

    def list(self) -> list[PaperRecord]:
        with self._scope() as s:
            return [_paper(r) for r in s.scalars(select(PaperRow).order_by(PaperRow.id))]


class SqlRunRepo(SqlRepository):
    def save(self, state: RunState) -> None:
        data = state.model_dump(mode="json")
        with self._scope() as s:
            row = s.get(PipelineRunRow, state.run_id)
            if row is None:
                s.add(PipelineRunRow(run_id=state.run_id, paper_id=state.paper_id, status=state.status, node=state.node, state=data,
                                     created_at=state.created_at, updated_at=state.updated_at))
            else:
                row.status, row.node, row.state, row.updated_at = state.status, state.node, data, state.updated_at

    def get(self, run_id: str) -> RunState | None:
        with self._scope() as s:
            r = s.get(PipelineRunRow, run_id)
            return None if r is None else RunState.model_validate(r.state)

    def list(self, paper_id: str | None = None) -> list[RunState]:
        q = select(PipelineRunRow)
        if paper_id:
            q = q.where(PipelineRunRow.paper_id == paper_id)
        with self._scope() as s:
            return [RunState.model_validate(r.state) for r in s.scalars(q.order_by(PipelineRunRow.run_id))]


class SqlGateRepo(SqlRepository):
    @staticmethod
    def _to_model(r: GateRow) -> Gate:
        return Gate(id=r.id, run_id=r.run_id, kind=r.kind, status=r.status, context=r.context,  # type: ignore[arg-type]
                    allowed_actions=r.allowed_actions, action=r.action, response=r.response, created_at=r.created_at,
                    resolved_at=r.resolved_at)

    def create(self, gate: Gate) -> None:
        with self._scope() as s:
            s.add(GateRow(**gate.model_dump()))

    def get(self, gate_id: str) -> Gate | None:
        with self._scope() as s:
            r = s.get(GateRow, gate_id)
            return None if r is None else self._to_model(r)

    def update(self, gate: Gate) -> None:
        with self._scope() as s:
            r = s.get(GateRow, gate.id)
            if r is None:
                raise KeyError(gate.id)
            r.status, r.action, r.response, r.resolved_at = gate.status, gate.action, gate.response, gate.resolved_at

    def list(self, *, run_id: str | None = None, status: str | None = None) -> list[Gate]:
        q = select(GateRow)
        if run_id:
            q = q.where(GateRow.run_id == run_id)
        if status:
            q = q.where(GateRow.status == status)
        with self._scope() as s:
            return [self._to_model(r) for r in s.scalars(q.order_by(GateRow.id))]
