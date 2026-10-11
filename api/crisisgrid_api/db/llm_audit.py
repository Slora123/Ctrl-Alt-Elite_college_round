from __future__ import annotations

from sqlalchemy import delete, select

from ..gateway.types import LLMCallRecord, Message
from .base import SqlRepository
from .tables import LLMCallRow


class SqlAuditLog(SqlRepository):
    """LLMAuditLog: prompts, outputs and model versions for every call."""

    def append(self, record: LLMCallRecord) -> None:
        row = record.model_dump(mode="json")
        row["ts"] = record.ts
        with self._scope() as s:
            s.add(LLMCallRow(**row))

    def delete_runs(self, run_ids: list[str]) -> None:
        """The prompts contain the paper's text, so they are erased with the paper."""
        if run_ids:
            with self._scope() as s:
                s.execute(delete(LLMCallRow).where(LLMCallRow.run_id.in_(run_ids)))

    def list(self, *, run_id: str | None = None, limit: int = 1000) -> list[LLMCallRecord]:
        q = select(LLMCallRow)
        if run_id is not None:
            q = q.where(LLMCallRow.run_id == run_id)
        with self._scope() as s:
            return [
                LLMCallRecord(
                    id=r.id, ts=r.ts, run_id=r.run_id, agent=r.agent, provider=r.provider, model=r.model,
                    messages=[Message(**m) for m in r.messages], response=r.response, prompt_tokens=r.prompt_tokens,
                    completion_tokens=r.completion_tokens, cached=r.cached, error=r.error,
                )
                for r in s.scalars(q.order_by(LLMCallRow.id).limit(limit))
            ]
