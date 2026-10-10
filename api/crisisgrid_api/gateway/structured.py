"""Typed agent output: free text is never parsed, only validated JSON. Invalid output gets one retry (with the
validation error shown to the model), then the Harness guard escalates."""
from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from crisisgrid_core.harness import EscalationRequired, HarnessGuard

from .gateway import Gateway
from .types import Message

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def extract_json(text: str) -> object:
    t = text.strip()
    m = _FENCE.match(t)
    if m:
        t = m.group(1)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:  # tolerate prose around a single JSON object
            return json.loads(t[start:end + 1])
        raise


def schema_instruction(output_type: type[BaseModel]) -> str:
    return ("Respond with ONLY a JSON object that conforms to this JSON Schema, no prose:\n"
            + json.dumps(output_type.model_json_schema(), separators=(",", ":")))


def run_structured(gateway: Gateway, guard: HarnessGuard, stage: str, agent: str, messages: list[Message],
                   output_type: type[T], *, run_id: str | None = None) -> T:
    """Call the model and return a validated `output_type`. Raises EscalationRequired when the guard says stop."""
    msgs = [*messages, Message(role="system", content=schema_instruction(output_type))]
    guard.reset_validation(stage)
    while True:
        resp = gateway.complete(agent, msgs, run_id=run_id)
        d = guard.charge_tokens(stage, resp.total_tokens)
        if not d.ok:
            raise EscalationRequired(d.reason)
        try:
            return output_type.model_validate(extract_json(resp.text))
        except (ValidationError, ValueError) as e:  # json.JSONDecodeError is a ValueError
            gateway.evict(agent, msgs)  # never serve this invalid reply from the cache again
            d = guard.record_validation_failure(stage)
            if not d.ok:
                raise EscalationRequired(d.reason) from e
            msgs = [*msgs, Message(role="assistant", content=resp.text),
                    Message(role="user", content=f"Your reply was invalid: {str(e)[:500]}\nReturn corrected JSON only.")]
