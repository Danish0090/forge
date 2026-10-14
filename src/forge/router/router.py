"""Model router: one config line picks the model; all model API calls happen on the host.

The router is the only place that touches API keys. Keys come from the host environment, are passed
per call, and never enter the sandbox. Model output is untrusted data; callers must validate it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


class RouterError(Exception):
    """A model call failed or returned something unusable."""


@dataclass(frozen=True)
class ModelSpec:
    litellm_model: str
    key_env: str | None = None  # None: no key needed (local Ollama)
    api_base: str | None = None


OLLAMA_URL = "http://localhost:11434"  # fixed: model traffic stays on this machine
_OLLAMA_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


# OpenRouter slugs checked against its public model list on 2026-10-14. They drift: if a call 404s,
# re-read https://openrouter.ai/api/v1/models. Free open-weight models only (no paid keys needed).
MODELS: dict[str, ModelSpec] = {
    "nemotron": ModelSpec("openrouter/nvidia/nemotron-3-super-120b-a12b:free", "OPENROUTER_API_KEY"),
    "gemma": ModelSpec("openrouter/google/gemma-4-31b-it:free", "OPENROUTER_API_KEY"),
    "gemma-26b": ModelSpec("openrouter/google/gemma-4-26b-a4b-it:free", "OPENROUTER_API_KEY"),
    "nemotron-ultra": ModelSpec("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "OPENROUTER_API_KEY"),
    "nemotron-nano": ModelSpec("openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "OPENROUTER_API_KEY"),
    "nemotron-lightning": ModelSpec("openrouter/nvidia/nemotron-3.5-lightning:free", "OPENROUTER_API_KEY"),
}


def resolve_model(name: str) -> ModelSpec:
    """An alias from MODELS, or `ollama/<tag>` for any model pulled into local Ollama."""
    if name.startswith("ollama/"):
        tag = name.removeprefix("ollama/")
        if not _OLLAMA_TAG_RE.fullmatch(tag) or ".." in tag:
            raise RouterError(f"invalid Ollama model tag {tag!r}")
        return ModelSpec(f"ollama_chat/{tag}", None, OLLAMA_URL)
    try:
        return MODELS[name]
    except KeyError:
        raise RouterError(f"unknown model {name!r}; choose one of {sorted(MODELS)}") from None


def _default_completion(**kwargs: Any) -> Any:
    import litellm  # lazy: importing it is slow and tests never need it

    return litellm.completion(**kwargs)


class ModelRouter:
    def __init__(
        self,
        model: str,
        *,
        completion: Callable[..., Any] = _default_completion,
        env: Mapping[str, str] = os.environ,
        max_tokens: int = 4_000,
        timeout_s: float = 120.0,
        temperature: float = 0.2,
    ) -> None:
        if max_tokens <= 0 or timeout_s <= 0:
            raise RouterError("max_tokens and timeout_s must be positive")
        self._spec = resolve_model(model)
        self._completion = completion
        self._env = env
        self._max_tokens = max_tokens
        self._timeout_s = timeout_s
        self._temperature = temperature

    def complete(self, system: str, user: str) -> str:
        key = ""
        extra: dict[str, Any] = {}
        if self._spec.key_env:
            key = self._env.get(self._spec.key_env, "")
            if not key:
                raise RouterError(f"{self._spec.key_env} is not set in the host environment")
            extra["api_key"] = key
        if self._spec.api_base:
            extra["api_base"] = self._spec.api_base
        try:
            response = self._completion(
                model=self._spec.litellm_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=self._max_tokens,
                timeout=self._timeout_s,
                temperature=self._temperature,
                **extra,
            )
            text = response["choices"][0]["message"]["content"]
        except Exception as exc:
            raise RouterError(f"model call failed: {type(exc).__name__}: {str(exc).replace(key, "***") if key else str(exc)}") from None
        if not isinstance(text, str) or not text.strip():
            raise RouterError("model returned an empty reply")
        return text
