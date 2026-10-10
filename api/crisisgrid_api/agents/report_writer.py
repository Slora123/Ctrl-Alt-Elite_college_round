"""Report writer agent: a short narrative over the evidence. It is one of only two agents allowed to see claim values (the
other is the Claim verifier). Its output is machine-checked: every number it writes must exist in the evidence."""
from __future__ import annotations

import json

from pydantic import BaseModel, ValidationInfo, field_validator

from crisisgrid_core.harness import HarnessGuard
from crisisgrid_core.models import Claim, Gap, Verdict
from crisisgrid_core.report_check import allowed_numbers, check_numbers

from ..gateway.gateway import Gateway
from ..gateway.prompts import SYSTEM_RULES
from ..gateway.structured import run_structured
from ..gateway.types import Message

INSTRUCTIONS = """You write a short, plain summary of reproduction results for a researcher.
Rules:
- Use ONLY the facts given. Copy every number exactly as given; never compute, round differently or invent a number.
- Say what was reproduced, partially reproduced, not reproduced or inconclusive, and why, using each verdict's caveats.
- Never say a paper is wrong or fraudulent. A result that did not reproduce is a finding about THIS run, with its caveats.
- Reply as JSON: {"summary": "<3-6 sentences>", "limitations": ["<short sentence>", ...]}."""


class Narrative(BaseModel):
    summary: str
    limitations: list[str] = []

    @field_validator("summary", "limitations", mode="after")
    @classmethod
    def _numbers_must_come_from_the_evidence(cls, v: str | list[str], info: ValidationInfo) -> str | list[str]:
        allowed = (info.context or {}).get("allowed")
        if allowed is None:
            return v
        text = v if isinstance(v, str) else " ".join(v)
        bad = check_numbers(text, allowed)
        if bad:
            raise ValueError(f"these numbers are not in the evidence: {bad}. Use only the numbers provided.")
        return v


class ReportWriterAgent:
    def __init__(self, gateway: Gateway, guard: HarnessGuard) -> None:
        self.gateway, self.guard = gateway, guard

    def write(self, claims: list[Claim], verdicts: list[Verdict], gaps: list[Gap], *, run_id: str | None = None) -> str:
        by_id = {c.id: c for c in claims}
        facts = [{"claim": by_id[v.claim_id].text if v.claim_id in by_id else v.claim_id,
                  **v.model_dump(mode="json", exclude={"claim_id", "evidence_run_ids"})} for v in verdicts]
        counts = {s: sum(g.severity == s for g in gaps) for s in ("blocking", "major", "minor")}
        extra = [float(len(verdicts)), float(len(gaps)), *map(float, counts.values())]
        msgs = [Message(role="system", content=SYSTEM_RULES + "\n" + INSTRUCTIONS),
                Message(role="user", content="Verdicts:\n" + json.dumps(facts) + "\nGap counts by severity: " + json.dumps(counts))]
        self.guard.start_stage("report")
        n = run_structured(self.gateway, self.guard, "report", "report_writer", msgs, Narrative, run_id=run_id,
                           context={"allowed": allowed_numbers(verdicts, extra=extra)})
        return n.summary + ("\n\n" + "\n".join(f"- {x}" for x in n.limitations) if n.limitations else "")
