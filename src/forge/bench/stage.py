"""Where a forge attempt stopped, read from the orchestrator's observation text.

The orchestrator reports failures as plain text ("checks failed: ...", "staging failed: ...").
This module only classifies that text for reporting; it decides nothing about trust.
"""

from __future__ import annotations

from typing import Literal

Stage = Literal["checks", "staging", "approval", "forger", "other"]


def stage_of_failure(text: str) -> Stage:
    """Classify a failed forge observation. `approval` means the human gate said no."""
    if text.startswith("checks failed"):
        return "checks"
    if text.startswith("staging"):
        return "staging"
    if text.startswith("forger failed"):
        return "forger"
    if text in ("rejected by reviewer", "no answer from a human") or text.startswith("tool files changed"):
        return "approval"
    return "other"
