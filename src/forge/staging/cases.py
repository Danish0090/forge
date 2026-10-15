"""Forger-written test cases: data the host checks, never code the host runs.

A case holds tiny fixture files, the arguments to call the tool with, and the exact output it
should print. The forger is a model and untrusted, so everything here is validated strictly and
the case never becomes code: the host writes the fixtures into a fresh folder, mounts it read-only
as /inputs in a throwaway sandbox, runs the already-scanned tool and compares its stdout.

The tests are evidence the tool does what its author says, not proof it is right: the same model
wrote the tool and the expected values, so a wrong idea can pass its own tests. The benchmark's
hidden test set is the independent check.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.checks.finding import Finding
from forge.checks.hash_pin import tree_hash
from forge.sandbox.policy import SandboxPolicy
from forge.sandbox.runner import run_in_sandbox
from forge.staging.manifest import Manifest, input_view
from forge.staging.trace import StagingTrace, make_trace

MAX_CASES = 5
MAX_FILES = 5
MAX_FILE_CHARS = 4_000
MAX_EXPECT_CHARS = 200
_CLIP = 80

_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class CaseError(ValueError):
    """A test case is malformed. The forger gets the message and may try again."""


@dataclass(frozen=True)
class StagingCase:
    args: dict[str, Any]
    expect: str
    files: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CaseReport:
    findings: tuple[Finding, ...]
    run: int
    passed: int
    traces: tuple[StagingTrace, ...] = ()


def parse_cases(raw: object) -> tuple[StagingCase, ...]:
    """Turn untrusted JSON into cases. Anything unexpected raises CaseError."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CaseError("'tests' must be a list")
    if len(raw) > MAX_CASES:
        raise CaseError(f"at most {MAX_CASES} tests")
    cases = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise CaseError(f"test {i} must be an object")
        case = StagingCase(
            args=item.get("args", {}) if isinstance(item.get("args", {}), dict) else _bad(i, "'args' must be an object"),
            expect=item.get("expect") if isinstance(item.get("expect"), str) else _bad(i, "'expect' must be a string"),
            files=item.get("files", {}) if isinstance(item.get("files", {}), dict) else _bad(i, "'files' must be an object"),
        )
        _validate(case, i)
        cases.append(case)
    return tuple(cases)


def _bad(i: int, why: str) -> Any:
    raise CaseError(f"test {i}: {why}")


def _validate(case: StagingCase, i: int) -> None:
    if not case.expect.strip():
        raise CaseError(f"test {i}: 'expect' must not be empty")
    if len(case.expect) > MAX_EXPECT_CHARS:
        raise CaseError(f"test {i}: 'expect' is longer than {MAX_EXPECT_CHARS} characters")
    if len(case.files) > MAX_FILES:
        raise CaseError(f"test {i}: at most {MAX_FILES} files")
    for name, content in case.files.items():
        if not isinstance(name, str) or not _FILE_NAME.fullmatch(name):
            raise CaseError(f"test {i}: file name {str(name)[:40]!r} must be plain letters, digits, '.', '_' or '-'")
        if not isinstance(content, str):
            raise CaseError(f"test {i}: file {name!r} must hold text")
        if len(content) > MAX_FILE_CHARS:
            raise CaseError(f"test {i}: file {name!r} is longer than {MAX_FILE_CHARS} characters")


def _normal(text: str) -> str:
    return " ".join(text.split())


def _clip(text: str) -> str:
    return text if len(text) <= _CLIP else text[:_CLIP] + "..."


def run_cases(
    image: str,
    tool_dir: Path,
    cases: Sequence[StagingCase],
    command_for: Callable[[dict[str, Any]], list[str] | None],
    *,
    timeout_s: float = 10.0,
    manifest: Manifest | None = None,
) -> CaseReport:
    """Run every case in its own throwaway sandbox. A case passes when stdout matches after whitespace is normalised."""
    findings: list[Finding] = []
    traces: list[StagingTrace] = []
    passed = 0
    if not cases:
        return CaseReport((), 0, 0)
    tool_before = tree_hash(tool_dir)
    for n, case in enumerate(cases, start=1):
        try:
            _validate(case, n)
        except CaseError as exc:
            findings.append(Finding("staging-test-invalid", str(exc)))
            continue
        command = command_for(case.args)
        if command is None:
            findings.append(Finding("staging-test-invalid", f"case {n}: arguments are not JSON or too large"))
            continue
        fixtures = Path(tempfile.mkdtemp(prefix="forge-case-"))
        try:
            fixtures.chmod(0o755)
            for name, content in case.files.items():
                path = fixtures / name
                path.write_text(content)
                path.chmod(0o644)
            before = tree_hash(fixtures)
            started = time.monotonic()
            with input_view(manifest, fixtures) as view:  # a case sees only what the manifest declares, like the real run
                result = run_in_sandbox(SandboxPolicy(image=image, tool_dir=tool_dir, inputs_dir=view.path, timeout_s=timeout_s), command)
            traces.append(make_trace(f"case {n}", command, result, time.monotonic() - started))
            if tree_hash(fixtures) != before:
                findings.append(Finding("staging-inputs-modified", f"case {n}: fixture files changed during the run"))
        finally:
            shutil.rmtree(fixtures, ignore_errors=True)
        if result.timed_out:
            findings.append(Finding("staging-test-error", f"case {n}: run exceeded {timeout_s}s and was killed"))
        elif result.exit_code != 0:
            findings.append(Finding("staging-test-error", f"case {n}: tool exited with code {result.exit_code}"))
        elif _normal(result.stdout) != _normal(case.expect):
            findings.append(
                Finding("staging-test-failed", f"case {n}: expected {_clip(_normal(case.expect))!r}, got {_clip(_normal(result.stdout))!r}")
            )
        else:
            passed += 1
    if tree_hash(tool_dir) != tool_before:
        findings.append(Finding("staging-tool-modified", "tool files changed while the tests ran"))
    return CaseReport(tuple(findings), len(cases), passed, tuple(traces))
