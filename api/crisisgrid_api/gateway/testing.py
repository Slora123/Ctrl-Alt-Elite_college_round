"""Scripted fake model client for tests and offline demos (deterministic, no network)."""
from __future__ import annotations

from collections.abc import Callable

from .types import LLMResponse, Message, ProviderError

Step = str | ProviderError | Callable[[list[Message]], str]


class ScriptedClient:
    """Plays back `steps` in order; the last step repeats. A str is the reply, a ProviderError is raised, a callable
    computes the reply from the messages."""

    def __init__(self, *steps: Step, tokens: int = 10) -> None:
        self._steps = list(steps) or [""]
        self.calls: list[list[Message]] = []
        self._tokens = tokens

    def complete(self, model: str, messages: list[Message], *, temperature: float, max_tokens: int) -> LLMResponse:
        step = self._steps[min(len(self.calls), len(self._steps) - 1)]
        self.calls.append(messages)
        if isinstance(step, ProviderError):
            raise step
        text = step(messages) if callable(step) else step
        return LLMResponse(text=text, model=model, prompt_tokens=self._tokens, completion_tokens=self._tokens)
