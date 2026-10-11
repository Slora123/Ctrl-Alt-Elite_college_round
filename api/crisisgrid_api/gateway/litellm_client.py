"""LiteLLM adapter (Qwen on Groq / Ollama Cloud, BYO keys, local Ollama). NOT exercised by the test suite: it needs
`pip install litellm` and a provider key. Everything else talks to the ModelClient protocol."""
from __future__ import annotations

import re

from .types import LLMResponse, Message, ProviderError

# Models served behind an OpenAI-compatible endpoint that accept chat_template_kwargs.enable_thinking (vLLM/NIM templates)
NO_THINK = re.compile(r"^(nvidia_nim|hosted_vllm|openai)/.*(nemotron-3|qwen3)", re.I)


class LiteLLMClient:
    def __init__(self, api_key: str | None = None, api_base: str | None = None, timeout: float = 240.0) -> None:
        self._api_key, self._api_base, self._timeout = api_key, api_base, timeout
        # Import once here (app startup, main thread): importing lazily inside complete() let several pipeline threads race
        # on litellm's first import and deadlock (_DeadlockError). Tolerate a missing package so the app still starts.
        try:
            import litellm  # noqa: F401
        except ImportError:
            pass

    def complete(self, model: str, messages: list[Message], *, temperature: float, max_tokens: int) -> LLMResponse:
        import litellm  # lazy: optional dependency

        extra = {}
        if NO_THINK.search(model):  # hybrid reasoning models: thinking eats the output budget and we only want the JSON
            extra["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        try:
            r = litellm.completion(
                model=model, messages=[m.model_dump() for m in messages], temperature=temperature,
                max_tokens=max_tokens, api_key=self._api_key, api_base=self._api_base, timeout=self._timeout, **extra,
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
        text = r.choices[0].message.content or ""
        if not text.strip():
            finish = getattr(r.choices[0], "finish_reason", None)
            # typical with reasoning models (e.g. gpt-oss): the whole output budget went into hidden reasoning
            raise ProviderError("invalid", f"{model} returned an empty answer (finish_reason={finish}). If this is a reasoning "
                                "model it spent the whole output budget thinking: raise CG_LLM_MAX_TOKENS or use a non-reasoning "
                                "instruct model (e.g. nvidia_nim/nvidia/nemotron-3-super-120b-a12b, thinking is switched off for it).")
        return LLMResponse(
            text=text, model=getattr(r, "model", model) or model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0, completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
