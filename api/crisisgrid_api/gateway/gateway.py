"""Model gateway (system_design 6): provider chain, backoff with jitter, cooling on 429, deterministic-call cache,
per-agent profiles, audit logging. Clock, sleep and RNG are injectable so tests are instant and deterministic."""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, Field

from ..repos.llm_memory import InMemoryAuditLog, InMemoryCache
from .types import (
    AllProvidersFailed,
    Cache,
    LLMAuditLog,
    LLMCallRecord,
    LLMResponse,
    Message,
    ModelClient,
    ProviderError,
)

log = logging.getLogger("crisisgrid.gateway")


@dataclass
class Provider:
    name: str
    model: str
    client: ModelClient


class AgentProfile(BaseModel):
    """Per-agent settings: which providers, in what order, and sampling."""

    providers: list[str] = Field(default_factory=list)  # empty = the gateway's full chain, in order
    temperature: float = 0.0
    max_tokens: int = 2048


class Gateway:
    def __init__(
        self,
        providers: list[Provider],
        profiles: dict[str, AgentProfile] | None = None,
        *,
        cache: Cache | None = None,
        audit: LLMAuditLog | None = None,
        max_retries: int = 2,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        default_cooldown: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._providers = {p.name: p for p in providers}
        self._order = [p.name for p in providers]
        self._profiles = profiles or {}
        self.cache: Cache = cache if cache is not None else InMemoryCache()
        self.audit: LLMAuditLog = audit if audit is not None else InMemoryAuditLog()
        self._max_retries = max_retries
        self._base, self._cap, self._cooldown = backoff_base, backoff_cap, default_cooldown
        self._sleep, self._clock = sleep, clock
        self._rng = rng or random.Random()
        self._cool_until: dict[str, float] = {}

    def profile(self, agent: str) -> AgentProfile:
        return self._profiles.get(agent) or self._profiles.get("default") or AgentProfile()

    def _chain(self, profile: AgentProfile) -> list[Provider]:
        names = profile.providers or self._order
        return [self._providers[n] for n in names if n in self._providers]

    @staticmethod
    def _cache_key(agent: str, model_chain: list[str], messages: list[Message], temperature: float, max_tokens: int) -> str:
        blob = json.dumps({"a": agent, "m": model_chain, "msgs": [m.model_dump() for m in messages], "t": temperature,
                           "n": max_tokens}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def evict(self, agent: str, messages: list[Message], *, temperature: float | None = None, max_tokens: int | None = None) -> None:
        """Forget a cached answer, e.g. one that turned out not to validate, so a retry is not served the same bad reply."""
        prof = self.profile(agent)
        temp = prof.temperature if temperature is None else temperature
        maxt = prof.max_tokens if max_tokens is None else max_tokens
        chain = [f"{p.name}:{p.model}" for p in self._chain(prof)]
        self.cache.delete(self._cache_key(agent, chain, messages, temp, maxt))

    def _backoff(self, attempt: int) -> float:
        return self._rng.uniform(0, min(self._cap, self._base * 2**attempt))  # full jitter

    def complete(self, agent: str, messages: list[Message], *, run_id: str | None = None,
                 temperature: float | None = None, max_tokens: int | None = None) -> LLMResponse:
        prof = self.profile(agent)
        temp = prof.temperature if temperature is None else temperature
        maxt = prof.max_tokens if max_tokens is None else max_tokens
        chain = self._chain(prof)
        key = self._cache_key(agent, [f"{p.name}:{p.model}" for p in chain], messages, temp, maxt)

        if temp == 0:  # only deterministic calls are cached
            hit = self.cache.get(key)
            if hit is not None:
                hit = hit.model_copy(update={"cached": True})
                self.audit.append(LLMCallRecord(run_id=run_id, agent=agent, provider=hit.provider, model=hit.model,
                                                messages=messages, response=hit.text, cached=True))
                return hit

        errors: list[str] = []
        for p in chain:
            if self._cool_until.get(p.name, 0.0) > self._clock():
                errors.append(f"{p.name}: cooling")
                continue
            for attempt in range(self._max_retries + 1):
                try:
                    resp = p.client.complete(p.model, messages, temperature=temp, max_tokens=maxt)
                except ProviderError as e:
                    self.audit.append(LLMCallRecord(run_id=run_id, agent=agent, provider=p.name, model=p.model,
                                                    messages=messages, error=str(e)))
                    errors.append(f"{p.name}: {e}")
                    if e.kind == "rate_limit":
                        self._cool_until[p.name] = self._clock() + (e.retry_after or self._cooldown)
                        break  # fail over immediately, no backoff on a provider that told us to go away
                    if e.kind in ("auth", "invalid"):
                        break
                    if attempt < self._max_retries:  # timeout / server: back off and retry the same provider
                        self._sleep(self._backoff(attempt))
                    continue
                resp = resp.model_copy(update={"provider": p.name, "model": resp.model or p.model})
                self.audit.append(LLMCallRecord(
                    run_id=run_id, agent=agent, provider=resp.provider, model=resp.model, messages=messages,
                    response=resp.text, prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens))
                if temp == 0:
                    self.cache.set(key, resp)
                return resp
        log.warning("all providers failed", extra={"agent": agent, "errors": errors})
        raise AllProvidersFailed(errors)
