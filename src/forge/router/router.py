"""Model router: one config line picks the model; all model API calls happen on the host.

The router is the only place that touches API keys. Keys come from the host environment, are passed
per call, and never enter the sandbox. Model output is untrusted data; callers must validate it.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class RouterError(Exception):
    """A model call failed or returned something unusable."""


@dataclass(frozen=True)
class ModelSpec:
    litellm_model: str
    key_env: str | None = None  # None: no key needed (local Ollama)
    api_base: str | None = None
    price_in: float = 0.0  # USD per million prompt tokens; 0 for free and local models
    price_out: float = 0.0  # USD per million completion tokens

    @property
    def paid(self) -> bool:
        return self.price_in > 0 or self.price_out > 0


OLLAMA_URL = "http://localhost:11434"  # fixed: model traffic stays on this machine
_OLLAMA_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


# OpenRouter slugs checked against its public model list on 2026-10-14. They drift: if a call 404s,
# re-read https://openrouter.ai/api/v1/models. Free ones need only a free key.
MODELS: dict[str, ModelSpec] = {
    "nemotron": ModelSpec("openrouter/nvidia/nemotron-3-super-120b-a12b:free", "OPENROUTER_API_KEY"),
    "gemma": ModelSpec("openrouter/google/gemma-4-31b-it:free", "OPENROUTER_API_KEY"),
    "gemma-26b": ModelSpec("openrouter/google/gemma-4-26b-a4b-it:free", "OPENROUTER_API_KEY"),
    "nemotron-ultra": ModelSpec("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "OPENROUTER_API_KEY"),
    "nemotron-nano": ModelSpec("openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "OPENROUTER_API_KEY"),
    "nemotron-lightning": ModelSpec("openrouter/nvidia/nemotron-3.5-lightning:free", "OPENROUTER_API_KEY"),
    # Paid open-weight models (owner approved, 2026-10-15). Prices are USD per million tokens from the
    # OpenRouter list on that date; they drift. They run only with allow_paid and a spend cap.
    "kimi-k2.7-code": ModelSpec("openrouter/moonshotai/kimi-k2.7-code", "OPENROUTER_API_KEY", None, 0.67, 3.35),
    "minimax-m3": ModelSpec("openrouter/minimax/minimax-m3", "OPENROUTER_API_KEY", None, 0.30, 1.20),
    "deepseek-v4-pro": ModelSpec("openrouter/deepseek/deepseek-v4-pro", "OPENROUTER_API_KEY", None, 0.41, 0.82),
    "qwen3-coder": ModelSpec("openrouter/qwen/qwen3-coder", "OPENROUTER_API_KEY", None, 0.30, 1.00),
    # Closed weights: the owner chose these (2026-10-15) as reference points.
    "gpt-5.6-sol": ModelSpec("openrouter/openai/gpt-5.6-sol", "OPENROUTER_API_KEY", None, 4.00, 20.00),
    "claude-opus-5": ModelSpec("openrouter/anthropic/claude-opus-5", "OPENROUTER_API_KEY", None, 5.00, 25.00),
    # Kept for reproducibility: on 2026-10-15 OpenRouter returned finish_reason content_filter with no
    # text for the forger prompt on this model (and on claude-fable-5.1), so it cannot be a forger.
    "claude-opus-5.5": ModelSpec("openrouter/anthropic/claude-opus-5.5", "OPENROUTER_API_KEY", None, 4.00, 20.00),
    "gemini-4-argon": ModelSpec("openrouter/google/gemini-4-argon", "OPENROUTER_API_KEY", None, 2.00, 10.00),
}

DEFAULT_MAX_COST_USD = 0.50

# On 2026-10-15 the token-based estimate summed to about 5 USD while the provider dashboard showed about
# 15 USD for the same work (hidden reasoning tokens and calls that fail after billing are not in the token
# counts). When the provider does not report a cost, the estimate is multiplied by this factor so the cap
# stays on the safe side. It is still an estimate: the provider's dashboard is the only real total.
COST_SAFETY_FACTOR = 3.0
HARD_MAX_COST_USD = 5.0

# Free tiers are often busy. Only "try again later" answers are retried; anything else fails fast.
_RETRY_STATUS = frozenset({429, 503})
MAX_RETRIES = 5


def _is_busy(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) in _RETRY_STATUS


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
        retry_waits: Sequence[float] = (5.0, 20.0),
        sleep: Callable[[float], None] = time.sleep,
        allow_paid: bool = False,
        max_cost_usd: float = DEFAULT_MAX_COST_USD,
    ) -> None:
        if not 0 < max_cost_usd <= HARD_MAX_COST_USD:
            raise RouterError(f"max_cost_usd must be above 0 and at most {HARD_MAX_COST_USD}")
        if max_tokens <= 0 or timeout_s <= 0:
            raise RouterError("max_tokens and timeout_s must be positive")
        if len(retry_waits) > MAX_RETRIES or any(w < 0 for w in retry_waits):
            raise RouterError(f"retry_waits must be at most {MAX_RETRIES} non-negative numbers")
        self._retry_waits = tuple(retry_waits)
        self._sleep = sleep
        self._spec = resolve_model(model)
        if self._spec.paid and not allow_paid:
            raise RouterError(f"{model!r} is a paid model; pass allow_paid to use it")
        self._max_cost = max_cost_usd
        self.spent_usd = 0.0  # what the cap is checked against: provider-reported cost, else a padded estimate
        self.priced_by_provider = 0  # calls whose cost the provider reported
        self.estimated_calls = 0  # calls priced by our estimate (padded by COST_SAFETY_FACTOR, or worst case when usage is unknown)
        self.failed_calls = 0  # calls that failed; they may still have been billed and are not priced
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
        if self.spent_usd >= self._max_cost:
            raise RouterError(f"spend cap reached (${self.spent_usd:.4f} of ${self._max_cost:.2f})")
        waits = iter(self._retry_waits)
        while True:
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
                self._add_cost(response)
                break
            except Exception as exc:
                wait = next(waits, None)
                if wait is not None and _is_busy(exc):
                    self._sleep(wait)
                    continue
                self.failed_calls += 1
                raise self._wrap(exc, key) from None
        if not isinstance(text, str) or not text.strip():
            raise RouterError("model returned an empty reply")
        return text

    def _add_cost(self, response: Any) -> None:
        if not self._spec.paid:
            return
        reported = _reported_cost(response)
        if reported is not None:
            self.spent_usd += reported
            self.priced_by_provider += 1
            return
        self.estimated_calls += 1
        try:
            usage = response["usage"]
            tokens_in, tokens_out = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
        except (KeyError, TypeError, ValueError, IndexError):
            # unknown usage is never free: charge a full reply at the output price (the worst case for the output)
            self.spent_usd += self._max_tokens * self._spec.price_out / 1e6
            return
        self.spent_usd += (tokens_in * self._spec.price_in + tokens_out * self._spec.price_out) / 1e6 * COST_SAFETY_FACTOR

    def spend_summary(self) -> str:
        """One line that says what the number is and is not. It is the cap basis, not the bill."""
        return (
            f"spend (cap basis, not the real bill): ${self.spent_usd:.4f}; {self.priced_by_provider} call(s) priced by the provider, "
            f"{self.estimated_calls} estimated (x{COST_SAFETY_FACTOR:g} safety factor, or worst case when usage was unknown), "
            f"{self.failed_calls} failed call(s) not priced. Check the provider dashboard for the real cost."
        )

    @staticmethod
    def _wrap(exc: Exception, key: str) -> RouterError:
        return RouterError(f"model call failed: {type(exc).__name__}: {str(exc).replace(key, "***") if key else str(exc)}")


def _reported_cost(response: Any) -> float | None:
    """The cost the provider billed for this call, when it says so (OpenRouter's usage.cost). None if absent or unusable."""
    try:
        value = response["usage"]["cost"]
    except (KeyError, TypeError, IndexError, AttributeError):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)
