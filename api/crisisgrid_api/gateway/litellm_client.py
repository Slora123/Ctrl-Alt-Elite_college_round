"""LiteLLM adapter (Qwen on Groq / Ollama Cloud, BYO keys, local Ollama). NOT exercised by the test suite: it needs
`pip install litellm` and a provider key. Everything else talks to the ModelClient protocol."""
from __future__ import annotations

from .types import LLMResponse, Message, ProviderError


class LiteLLMClient:
    def __init__(self, api_key: str | None = None, api_base: str | None = None, timeout: float = 60.0) -> None:
        self._api_key, self._api_base, self._timeout = api_key, api_base, timeout

    def complete(self, model: str, messages: list[Message], *, temperature: float, max_tokens: int) -> LLMResponse:
        import litellm  # lazy: optional dependency

        try:
            r = litellm.completion(
                model=model, messages=[m.model_dump() for m in messages], temperature=temperature,
                max_tokens=max_tokens, api_key=self._api_key, api_base=self._api_base, timeout=self._timeout,
            )
        except litellm.RateLimitError as e:
            raise ProviderError("rate_limit", str(e)) from e
        except litellm.Timeout as e:
            raise ProviderError("timeout", str(e)) from e
        except (litellm.AuthenticationError, litellm.PermissionDeniedError) as e:
            # includes e.g. Bedrock "your account is currently being verified": retrying will not help, fail over instead
            raise ProviderError("auth", str(e)) from e
        except litellm.NotFoundError as e:  # unknown / unavailable model id
            raise ProviderError("invalid", str(e)) from e
        except litellm.BadRequestError as e:
            raise ProviderError("invalid", str(e)) from e
        except Exception as e:  # noqa: BLE001  (service unavailable, connection errors, ...)
            raise ProviderError("server", str(e)) from e
        usage = getattr(r, "usage", None)
        return LLMResponse(
            text=r.choices[0].message.content or "", model=getattr(r, "model", model) or model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0, completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
