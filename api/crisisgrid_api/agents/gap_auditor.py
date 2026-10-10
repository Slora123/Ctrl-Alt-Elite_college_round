"""Gap auditor agent: finds methodology gaps the deterministic rules did not. It can never block on its own; merge_gaps
caps its severity at `major` and drops duplicates of rule gaps."""
from __future__ import annotations

from pydantic import BaseModel

from crisisgrid_core.audit_rules import AuditInput, merge_gaps, run_rules
from crisisgrid_core.harness import HarnessGuard
from crisisgrid_core.models import Gap
from crisisgrid_core.papers import ArtifactInventory, ParsedPaper

from ..gateway.gateway import Gateway
from ..gateway.prompts import SYSTEM_RULES, untrusted_block
from ..gateway.structured import run_structured
from ..gateway.types import Message

INSTRUCTIONS = """You audit an ML paper's methodology for reproducibility gaps. You are given the parsed setup and the
paper text. Report ONLY real gaps not already listed under 'known gaps': missing details a reproducer needs, evaluation
leakage, unclear baselines, undefined terms. Each gap: field (short snake_case name), severity (major or minor), description,
suggested_resolution, detected_by="agent". Return {"gaps": []} if there is nothing new."""


class GapList(BaseModel):
    gaps: list[Gap] = []


class GapAuditorAgent:
    def __init__(self, gateway: Gateway, guard: HarnessGuard) -> None:
        self.gateway, self.guard = gateway, guard

    def audit(self, parsed: ParsedPaper, inventory: ArtifactInventory, text: str, *, run_id: str | None = None,
              max_context_chars: int = 20_000, hint: str | None = None) -> list[Gap]:
        rule_gaps = run_rules(AuditInput(parsed, inventory, text))
        known = "\n".join(f"- {g.field}: {g.description}" for g in rule_gaps) or "(none)"
        # claim ids are random per parse; leave them out so an identical paper yields an identical (cacheable) prompt
        setup = parsed.model_dump_json(exclude={"dropped": True, "claims": {"__all__": {"id"}}})
        msgs = [
            Message(role="system", content=SYSTEM_RULES + "\n" + INSTRUCTIONS),
            Message(role="user", content="Known gaps:\n" + known + "\n\nParsed setup:\n" + setup),
            Message(role="user", content=untrusted_block("paper", text[:max_context_chars])),
        ]
        if hint:
            msgs.append(Message(role="user", content=f"Reviewer hint (from the human operator): {hint}"))
        self.guard.start_stage("audit")
        result = run_structured(self.gateway, self.guard, "audit", "gap_auditor", msgs, GapList, run_id=run_id)
        return merge_gaps(rule_gaps, result.gaps)
