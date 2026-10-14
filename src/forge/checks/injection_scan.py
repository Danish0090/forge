"""Prompt-injection scan of tool descriptions.

Heuristic. It catches known phrasings and invisible characters. It does not catch
every possible attack, so a clean scan is evidence, not proof.
"""

from __future__ import annotations

import re

from forge.checks.finding import Finding

_INJECTION = re.compile(
    r"ignore\s+(all\s+|the\s+)?(previous|prior|above)\s+instructions"
    r"|disregard\s+(all\s+|the\s+)?(previous|prior|above)\s+instructions"
    r"|you\s+are\s+now\b"
    r"|reveal\s+(the\s+)?system\s+prompt",
    re.IGNORECASE,
)

# Zero-width and bidirectional-control characters: invisible to a reviewer, visible to a model.
_HIDDEN = re.compile("[​-‏‪-‮⁠-⁤﻿]")


def scan_text(text: str) -> list[Finding]:
    """Return one Finding per suspicious match, with its line number."""
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for _ in _INJECTION.finditer(line):
            findings.append(Finding("prompt-injection", "text tries to override instructions", line=lineno))
        for _ in _HIDDEN.finditer(line):
            findings.append(Finding("hidden-unicode", "invisible control character", line=lineno))
    return findings
