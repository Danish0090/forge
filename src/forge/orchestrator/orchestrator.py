"""Orchestrator: the loop that ties the trust ladder together.

SECURITY-CRITICAL. Changes here need the project owner's review.

The planner (decides the next step) and the forger (writes a new tool) are model-backed and
therefore untrusted: their output is data. They get no sandbox, registry or approval access.
This module is the only caller of the ladder, and it never skips or reorders a stage:

    forger draft -> checks -> staging (sandbox) -> human approval -> registry -> callable

A tool is only ever run from the registry (re-hashed on every get), only in the sandbox, and
only through `run_in_sandbox`. Tool arguments travel as one JSON argv element, never a shell
string. Every failure (forger crash, rejected tool, tampered tool, bad call) becomes an
Observation for the planner; only an unavailable sandbox aborts the run.

Known limit: a tool's output is untrusted text handed back to the planner, so it can attempt
prompt injection. Output is length-capped here; the planner's prompt must treat it as data.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from forge.approval.gate import ask_human, request_approval
from forge.checks import run_checks
from forge.checks.hash_pin import tree_hash
from forge.registry import RegisteredTool, Registry, RegistryError
from forge.sandbox.policy import CODE_DIR, PolicyError, SandboxPolicy
from forge.sandbox.runner import run_in_sandbox
from forge.staging.cases import StagingCase
from forge.staging.manifest import Manifest, ManifestError, describe, input_view, load_manifest
from forge.staging.stager import run_staging
from forge.staging.trace import StagingTrace, summarize_traces

MAX_ARGS_BYTES = 8_192
MAX_OBSERVATION_CHARS = 4_000
_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class CallTool:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class NeedTool:
    need: str  # a capability gap, in words


@dataclass(frozen=True)
class Finish:
    answer: str


Action = CallTool | NeedTool | Finish


@dataclass(frozen=True)
class Observation:
    """What happened on one step. `text` is untrusted and length-capped."""

    kind: Literal["tool", "forge"]
    subject: str
    ok: bool
    text: str


@dataclass(frozen=True)
class ToolDraft:
    name: str
    tool_dir: Path  # absolute folder holding tool.py
    description: str
    staging_args: dict[str, Any] = field(default_factory=dict)
    tests: tuple[StagingCase, ...] = ()  # forger-written cases, run in staging (data, never code)


class Planner(Protocol):
    def next_action(
        self, task: str, tools: Sequence[RegisteredTool], history: Sequence[Observation]
    ) -> Action: ...


class Forger(Protocol):
    def forge(self, need: str, workdir: Path) -> ToolDraft: ...


@dataclass(frozen=True)
class RunResult:
    status: Literal["done", "max_steps"]
    answer: str | None
    forged: tuple[str, ...]
    history: tuple[Observation, ...]


class Orchestrator:
    def __init__(
        self,
        *,
        registry: Registry,
        planner: Planner,
        forger: Forger,
        image: str,
        inputs_dir: Path,
        workdir: Path,
        max_steps: int = 10,
        max_forges: int = 2,
        timeout_s: float = 10.0,
        require_tests: bool = False,
        require_manifest: bool = False,
        ask: Callable[[str], str] = ask_human,
        show: Callable[[str], None] = print,
    ) -> None:
        self._registry = registry
        self._planner = planner
        self._forger = forger
        self._image = image
        self._inputs_dir = inputs_dir
        self._workdir = workdir
        self._max_steps = max_steps
        self._max_forges = max_forges
        self._timeout_s = timeout_s
        self._require_tests = require_tests
        self._require_manifest = require_manifest
        self.traces: list[StagingTrace] = []  # every staging run of every attempt, for reports
        self._ask = ask
        self._show = show

    def run(self, task: str) -> RunResult:
        history: list[Observation] = []
        forged: list[str] = []
        forges_used = 0
        for _ in range(self._max_steps):
            tools = self._registry.list_tools()  # reload: new approvals appear on the next step
            action = self._planner.next_action(task, tools, tuple(history))
            if isinstance(action, Finish):
                return RunResult("done", action.answer, tuple(forged), tuple(history))
            if isinstance(action, CallTool):
                history.append(self._call(action))
            elif isinstance(action, NeedTool):
                if forges_used >= self._max_forges:
                    history.append(Observation("forge", action.need, False, "forge budget used up"))
                    continue
                forges_used += 1
                obs, name = self._acquire(action.need)
                history.append(obs)
                if name:
                    forged.append(name)
            else:
                history.append(Observation("tool", "?", False, "planner returned an unknown action"))
        return RunResult("max_steps", None, tuple(forged), tuple(history))

    def call_tool(self, name: str, args: dict[str, Any]) -> Observation:
        """Run one registered tool the same way a planner's call runs it: hash check, manifest view, sandbox.

        Used by the MCP server so an outside client gets exactly the same protections, from the same code.
        """
        return self._call(CallTool(name, args))

    def _acquire(self, need: str) -> tuple[Observation, str | None]:
        """Take one draft up the ladder. Returns the observation and the name if registered."""

        def fail(why: str) -> tuple[Observation, None]:
            return Observation("forge", need, False, _cap(why)), None

        self._workdir.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(dir=self._workdir, prefix="forge-"))
        try:
            try:
                draft = self._forger.forge(need, scratch)
            except Exception as exc:  # the forger is untrusted and model-backed
                return fail(f"forger failed: {exc}")
            command = _command(draft.staging_args)
            if command is None:
                return fail("staging args are not JSON or too large")
            if not draft.tool_dir.is_absolute() or not draft.tool_dir.is_dir():
                return fail("draft tool_dir is not an absolute directory")

            sha = tree_hash(draft.tool_dir)
            checks = run_checks(draft.tool_dir, draft.description, sha)
            if not checks.passed:
                return fail(f"checks failed: {_finding_names(checks.findings)}")

            try:
                manifest = load_manifest(draft.tool_dir)
            except ManifestError as exc:
                return fail(f"checks failed: manifest-invalid ({exc})")
            if manifest is None and self._require_manifest:
                return fail("checks failed: manifest-missing")
            if self._require_tests and not draft.tests:
                return fail("staging failed: the forger wrote no tests")
            try:
                report = run_staging(
                    self._image,
                    draft.tool_dir,
                    self._inputs_dir,
                    command,
                    timeout_s=self._timeout_s,
                    cases=draft.tests,
                    case_command=_command,
                    manifest=manifest,
                )
            except (PolicyError, ManifestError) as exc:
                return fail(f"staging could not start: {exc}")
            self.traces.extend(replace(t, tool=draft.name) for t in report.traces)
            if not report.passed:
                return fail(f"staging failed: {_finding_names(report.findings)}")
            if manifest is not None:
                self._show(f"Manifest: {describe(manifest)}")
            summary = summarize_traces(report.traces)
            if summary:
                self._show(summary)

            decision = request_approval(
                draft.tool_dir,
                draft.description,
                sha,
                report,
                previous_dir=self._previous_dir(draft.name),
                ask=self._ask,
                show=self._show,
            )
            if not decision.approved:
                return fail(decision.reason)
            try:
                entry = self._registry.register(draft.name, draft.tool_dir, draft.description, decision)
            except RegistryError as exc:
                return fail(f"registry refused: {exc}")
            note = f" ({report.cases_passed} own tests passed)" if report.cases_run else ""
            return Observation("forge", need, True, f"registered {entry.name} v{entry.version}{note}"), entry.name
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _previous_dir(self, name: str) -> Path | None:
        try:
            return self._registry.get(name).path
        except RegistryError:
            return None

    def _call(self, action: CallTool) -> Observation:
        def fail(why: str) -> Observation:
            return Observation("tool", action.name, False, _cap(why))

        if not _NAME_RE.fullmatch(action.name):
            return fail("invalid tool name")
        command = _command(action.args)
        if command is None:
            return fail("args are not JSON or too large")
        try:
            tool = self._registry.get(action.name)  # re-hashes the stored copy
        except RegistryError as exc:
            return fail(str(exc))
        try:
            manifest = load_manifest(tool.path)  # the registry copy, already hash-verified above
        except ManifestError as exc:
            return fail(f"{action.name} has an invalid manifest: {exc}")
        if manifest is None and self._require_manifest:
            return fail(f"{action.name} has no manifest")
        try:
            with input_view(manifest, self._inputs_dir) as view:
                policy = SandboxPolicy(
                    image=self._image,
                    tool_dir=tool.path,
                    inputs_dir=view.path,
                    timeout_s=self._timeout_s,
                )
                result = run_in_sandbox(policy, command)
        except (PolicyError, ManifestError) as exc:
            return fail(f"could not start: {exc}")

        if tree_hash(tool.path) != tool.sha256:
            return fail(f"{action.name} failed hash verification during the run; output discarded")
        if result.timed_out:
            return fail("tool timed out")
        if result.exit_code != 0:
            return fail(f"tool exited with code {result.exit_code}: {result.stderr}")
        suffix = " [output truncated]" if result.stdout_truncated else ""
        return Observation("tool", action.name, True, _cap(result.stdout + suffix))


def _command(args: dict[str, Any]) -> list[str] | None:
    """The fixed entry point plus the args as a single JSON argv element."""
    try:
        payload = json.dumps(args)
    except (TypeError, ValueError):
        return None
    if len(payload.encode()) > MAX_ARGS_BYTES:
        return None
    return ["python", f"{CODE_DIR}/tool.py", payload]


def _cap(text: str) -> str:
    return text if len(text) <= MAX_OBSERVATION_CHARS else text[:MAX_OBSERVATION_CHARS] + "…"


def _finding_names(findings: tuple) -> str:
    return ", ".join(sorted({f.check for f in findings}))
