"""Run a tool inside the sandbox against real inputs mounted read-only, and report evidence.

The sandbox is the boundary; this module only decides what counts as a clean run. It builds the
policy and calls the existing runner. It never starts a container itself and never weakens a flag.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.checks.finding import Finding
from forge.checks.hash_pin import tree_hash
from forge.sandbox.policy import SandboxPolicy
from forge.sandbox.runner import SandboxResult, run_in_sandbox
from forge.staging.cases import StagingCase, run_cases
from forge.staging.manifest import Manifest, input_view
from forge.staging.trace import StagingTrace, make_trace


@dataclass(frozen=True)
class StagingReport:
    findings: tuple[Finding, ...]
    result: SandboxResult
    cases_run: int = 0  # forger-written test cases that ran, and how many passed
    cases_passed: int = 0
    traces: tuple[StagingTrace, ...] = ()  # what each sandbox run did, for the reviewer and the reports

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
    cases: Sequence[StagingCase] = (),
    case_command: Callable[[dict[str, Any]], list[str] | None] | None = None,
    manifest: Manifest | None = None,
) -> StagingReport:
    """Run `command` once with `inputs_dir` at /inputs (read-only). A tool passes with zero findings.

    Besides the exit status, the tool and input folders are hashed before and after the run.
    The mount is read-only, so a change means the boundary failed and must be reported.

    Forger-written `cases` run afterwards, each in its own throwaway sandbox with its own read-only
    fixtures (see cases.py). They only run when this main run was clean, so a tool that already
    crashes on the real inputs is not run again. `case_command` builds the argv for a case's args.

    With a `manifest`, the sandbox mounts a view holding copies of only the declared files instead of
    `inputs_dir` (see manifest.py). The real folder is still hashed before and after.
    """
    if cases and case_command is None:
        raise ValueError("cases need a case_command to build the tool's argv")
    tool_before = tree_hash(tool_dir)
    inputs_before = tree_hash(inputs_dir)

    started = time.monotonic()
    with input_view(manifest, inputs_dir) as view:
        policy = SandboxPolicy(image=image, tool_dir=tool_dir, inputs_dir=view.path, timeout_s=timeout_s)
        result = run_in_sandbox(policy, list(command))
    traces = [make_trace("main", command, result, time.monotonic() - started)]

    findings: list[Finding] = [
        Finding("staging-manifest-missing-file", f"declared file {name!r} is not in the inputs") for name in view.missing
    ]
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
    run = passed = 0
    if cases and not findings:
        case_report = run_cases(image, tool_dir, cases, case_command, timeout_s=timeout_s, manifest=manifest)  # type: ignore[arg-type]
        findings.extend(case_report.findings)
        run, passed = case_report.run, case_report.passed
        traces.extend(case_report.traces)
    return StagingReport(findings=tuple(findings), result=result, cases_run=run, cases_passed=passed, traces=tuple(traces))
