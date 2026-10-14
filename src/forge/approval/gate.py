"""Stage 4 of the trust ladder: a human reads a diff and says yes or no.

SECURITY-CRITICAL. Changes here need the project owner's review.

Trust is evidence, never a claim. The gate does not ask a human until the evidence is clean:
it re-runs the checks itself (it does not trust a report handed to it) and requires a passing
staging report. A human cannot override failed evidence here; they fix the tool and start again.
Only an explicit "yes" approves. EOF, Ctrl-C, no terminal, or any other answer is a rejection.
The tool folder is hashed again after the human answers, so a swap while they read is caught.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from forge.approval.diff import escape_text, render_diff
from forge.checks import run_checks
from forge.checks.hash_pin import tree_hash
from forge.staging.stager import StagingReport

DEFAULT_MAX_DIFF_LINES = 2000


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    reason: str
    approved_sha256: str | None = None  # set only when approved: the exact tree the human saw


def ask_human(prompt: str) -> str:
    """Read an answer from a real terminal. Anything else (pipe, cron, agent) cannot approve."""
    if not sys.stdin.isatty():
        raise EOFError("approval needs an interactive terminal")
    return input(prompt)


def request_approval(
    tool_dir: Path,
    description: str,
    pinned_sha256: str,
    staging: StagingReport,
    *,
    previous_dir: Path | None = None,
    ask: Callable[[str], str] = ask_human,
    show: Callable[[str], None] = print,
    max_diff_lines: int = DEFAULT_MAX_DIFF_LINES,
) -> ApprovalDecision:
    """Show the reviewer the evidence and the diff, then record their decision."""
    checks = run_checks(tool_dir, description, pinned_sha256)
    if not checks.passed:
        return ApprovalDecision(False, f"checks failed: {_names(checks.findings)}")
    if not staging.passed:
        return ApprovalDecision(False, f"staging failed: {_names(staging.findings)}")

    diff = render_diff(previous_dir, tool_dir)
    if diff.count("\n") + 1 > max_diff_lines:
        return ApprovalDecision(False, f"diff too large to review ({max_diff_lines} line limit)")

    show(f"Tool hash (SHA-256): {pinned_sha256}")
    show(f"Description: {escape_text(description)}")
    show("Evidence: static scan, injection scan and hash pin clean; staging run clean.")
    show(diff)

    try:
        answer = ask("Type 'yes' to approve, anything else rejects: ")
    except (EOFError, KeyboardInterrupt):
        return ApprovalDecision(False, "no answer from a human")
    if answer.strip().lower() != "yes":
        return ApprovalDecision(False, "rejected by reviewer")

    if tree_hash(tool_dir) != pinned_sha256:
        return ApprovalDecision(False, "tool files changed while under review")
    return ApprovalDecision(True, "approved by reviewer", approved_sha256=pinned_sha256)


def _names(findings: tuple) -> str:
    return ", ".join(sorted({f.check for f in findings}))
