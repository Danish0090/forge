"""Run a tool inside the sandbox against real inputs mounted read-only, and report evidence.

The sandbox is the boundary; this module only decides what counts as a clean run. It builds the
policy and calls the existing runner. It never starts a container itself and never weakens a flag.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from forge.checks.finding import Finding
from forge.checks.hash_pin import tree_hash
from forge.sandbox.policy import SandboxPolicy
from forge.sandbox.runner import SandboxResult, run_in_sandbox


@dataclass(frozen=True)
class StagingReport:
    findings: tuple[Finding, ...]
    result: SandboxResult

    @property
    def passed(self) -> bool:
        return not self.findings


def run_staging(
    image: str,
    tool_dir: Path,
    inputs_dir: Path,
    command: Sequence[str],
    *,
    timeout_s: float = 10.0,
) -> StagingReport:
    """Run `command` once with `inputs_dir` at /inputs (read-only). A tool passes with zero findings.

    Besides the exit status, the tool and input folders are hashed before and after the run.
    The mount is read-only, so a change means the boundary failed and must be reported.
    """
    policy = SandboxPolicy(image=image, tool_dir=tool_dir, inputs_dir=inputs_dir, timeout_s=timeout_s)
    tool_before = tree_hash(tool_dir)
    inputs_before = tree_hash(inputs_dir)

    result = run_in_sandbox(policy, list(command))

    findings: list[Finding] = []
    if result.timed_out:
        findings.append(Finding("staging-timeout", f"run exceeded {timeout_s}s and was killed"))
    elif result.exit_code != 0:
        findings.append(Finding("staging-exit-code", f"tool exited with code {result.exit_code}"))
    if result.stdout_truncated or result.stderr_truncated:
        findings.append(Finding("staging-output-truncated", "output exceeded the size cap"))
    if tree_hash(inputs_dir) != inputs_before:
        findings.append(Finding("staging-inputs-modified", "input files changed during the run"))
    if tree_hash(tool_dir) != tool_before:
        findings.append(Finding("staging-tool-modified", "tool files changed during the run"))
    return StagingReport(findings=tuple(findings), result=result)
