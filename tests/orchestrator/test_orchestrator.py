"""Orchestrator tests. No Docker: the sandbox is faked, planner and forger are scripted."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from forge.orchestrator import orchestrator as orch_mod
from forge.orchestrator.orchestrator import (
    Action,
    CallTool,
    Finish,
    NeedTool,
    Observation,
    Orchestrator,
    ToolDraft,
)
from forge.registry import Registry, RegisteredTool
from forge.sandbox.policy import SandboxPolicy
from forge.sandbox.runner import SandboxResult
from forge.staging import stager

DIGEST = "python@sha256:" + "a" * 64
DESC = "Counts words in a text file."


def ok_result(**overrides: object) -> SandboxResult:
    base = SandboxResult("forge-x", 0, "42\n", "", False, False, False)
    return replace(base, **overrides)  # type: ignore[arg-type]


class ScriptedPlanner:
    def __init__(self, actions: list[Action]) -> None:
        self._actions = list(actions)
        self.seen_history: list[Sequence[Observation]] = []
        self.seen_tools: list[list[str]] = []

    def next_action(
        self, task: str, tools: Sequence[RegisteredTool], history: Sequence[Observation]
    ) -> Action:
        self.seen_history.append(list(history))
        self.seen_tools.append([t.name for t in tools])
        return self._actions.pop(0)


class ScriptedForger:
    def __init__(self, body: str = "print('42')\n", name: str = "word_count", desc: str = DESC) -> None:
        self.body, self.name, self.desc = body, name, desc
        self.needs: list[str] = []

    def forge(self, need: str, workdir: Path) -> ToolDraft:
        self.needs.append(need)
        d = workdir / f"draft{len(self.needs)}"
        d.mkdir()
        (d / "tool.py").write_text(self.body)
        return ToolDraft(self.name, d, self.desc, staging_args={})


@pytest.fixture
def sandbox_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[SandboxPolicy, list[str]]]:
    calls: list[tuple[SandboxPolicy, list[str]]] = []

    def fake(policy: SandboxPolicy, command: list[str]) -> SandboxResult:
        calls.append((policy, list(command)))
        return ok_result()

    monkeypatch.setattr(stager, "run_in_sandbox", fake)
    monkeypatch.setattr(orch_mod, "run_in_sandbox", fake)
    return calls


@pytest.fixture
def inputs(tmp_path: Path) -> Path:
    d = tmp_path / "inputs"
    d.mkdir()
    (d / "log.txt").write_text("a b c\n")
    return d


def build(tmp_path: Path, inputs: Path, planner: ScriptedPlanner, forger: ScriptedForger, answers=("yes",)):
    reg = Registry(tmp_path / "registry")
    given = iter(answers)
    shown: list[str] = []
    o = Orchestrator(
        registry=reg,
        planner=planner,
        forger=forger,
        image=DIGEST,
        inputs_dir=inputs,
        workdir=tmp_path / "work",
        ask=lambda _p: next(given),
        show=shown.append,
    )
    return o, reg, shown


def test_finishes_without_tools(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    o, _, _ = build(tmp_path, inputs, ScriptedPlanner([Finish("hi")]), ScriptedForger())
    r = o.run("say hi")
    assert r.status == "done" and r.answer == "hi" and r.forged == ()
    assert sandbox_calls == []


def test_gap_forges_approves_registers_then_calls_tool(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    planner = ScriptedPlanner([NeedTool("count words"), CallTool("word_count", {"file": "log.txt"}), Finish("3")])
    forger = ScriptedForger()
    o, reg, _ = build(tmp_path, inputs, planner, forger)
    r = o.run("how many words")
    assert r.status == "done" and r.answer == "3"
    assert r.forged == ("word_count",)
    assert forger.needs == ["count words"]
    assert reg.get("word_count").version == 1
    # the planner sees the new tool on the next step (reload)
    assert planner.seen_tools == [[], ["word_count"], ["word_count"]]
    # staging run then the real call, both in the sandbox with args as one JSON argv element
    assert len(sandbox_calls) == 2
    assert sandbox_calls[1][1] == ["python", "/app/tool.py", json.dumps({"file": "log.txt"})]
    assert sandbox_calls[1][0].tool_dir == reg.get("word_count").path
    assert sandbox_calls[1][0].inputs_dir == inputs
    assert planner.seen_history[2][-1].ok and "42" in planner.seen_history[2][-1].text


def test_rejected_tool_is_not_registered_and_planner_is_told(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    planner = ScriptedPlanner([NeedTool("count words"), Finish("gave up")])
    o, reg, _ = build(tmp_path, inputs, planner, ScriptedForger(), answers=("no",))
    r = o.run("t")
    assert r.forged == () and reg.list_tools() == []
    last = planner.seen_history[1][-1]
    assert not last.ok and "rejected by reviewer" in last.text


def test_failed_checks_never_reach_staging_or_human(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    planner = ScriptedPlanner([NeedTool("x"), Finish("no")])
    asked: list[str] = []
    forger = ScriptedForger(body="import socket\n")
    o, reg, _ = build(tmp_path, inputs, planner, forger)
    o._ask = lambda p: asked.append(p) or "yes"  # type: ignore[attr-defined]
    o.run("t")
    assert asked == [] and sandbox_calls == [] and reg.list_tools() == []
    assert "checks failed" in planner.seen_history[1][-1].text


def test_failed_staging_never_reaches_human(tmp_path: Path, inputs: Path, monkeypatch, sandbox_calls) -> None:
    bad = lambda policy, command: ok_result(exit_code=1)  # noqa: E731
    monkeypatch.setattr(stager, "run_in_sandbox", bad)
    planner = ScriptedPlanner([NeedTool("x"), Finish("no")])
    asked: list[str] = []
    o, reg, _ = build(tmp_path, inputs, planner, ScriptedForger())
    o._ask = lambda p: asked.append(p) or "yes"  # type: ignore[attr-defined]
    o.run("t")
    assert asked == [] and reg.list_tools() == []
    assert "staging failed" in planner.seen_history[1][-1].text


def test_forger_crash_is_an_observation_not_an_exception(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    class Boom(ScriptedForger):
        def forge(self, need: str, workdir: Path) -> ToolDraft:
            raise RuntimeError("model down")

    planner = ScriptedPlanner([NeedTool("x"), Finish("no")])
    o, _, _ = build(tmp_path, inputs, planner, Boom())
    assert o.run("t").status == "done"
    assert "model down" in planner.seen_history[1][-1].text


def test_forge_budget_is_enforced(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    planner = ScriptedPlanner([NeedTool("a"), NeedTool("b"), NeedTool("c"), Finish("x")])
    forger = ScriptedForger()
    o, _, _ = build(tmp_path, inputs, planner, forger, answers=("no", "no", "no"))
    o._max_forges = 2  # type: ignore[attr-defined]
    o.run("t")
    assert len(forger.needs) == 2
    assert "forge budget" in planner.seen_history[3][-1].text


def test_step_limit(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    planner = ScriptedPlanner([CallTool("nope", {})] * 3)
    o, _, _ = build(tmp_path, inputs, planner, ScriptedForger())
    o._max_steps = 3  # type: ignore[attr-defined]
    r = o.run("t")
    assert r.status == "max_steps" and r.answer is None


def test_unknown_or_invalid_tool_call_is_a_failed_observation(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    planner = ScriptedPlanner([CallTool("../etc", {}), CallTool("missing", {}), Finish("ok")])
    o, _, _ = build(tmp_path, inputs, planner, ScriptedForger())
    o.run("t")
    assert not planner.seen_history[1][-1].ok
    assert not planner.seen_history[2][-1].ok
    assert sandbox_calls == []


def test_tampered_registry_tool_is_not_run(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    o, reg, _ = build(tmp_path, inputs, ScriptedPlanner([NeedTool("x"), Finish("ok")]), ScriptedForger())
    o.run("t")
    (reg.get("word_count").path / "tool.py").write_text("print('evil')\n")
    planner = ScriptedPlanner([CallTool("word_count", {}), Finish("ok")])
    o._planner = planner  # type: ignore[attr-defined]
    before = len(sandbox_calls)
    o.run("t")
    assert len(sandbox_calls) == before
    assert not planner.seen_history[1][-1].ok
    assert "verification" in planner.seen_history[1][-1].text


def test_nonzero_exit_and_timeout_are_failed_observations(tmp_path: Path, inputs: Path, monkeypatch, sandbox_calls) -> None:
    planner = ScriptedPlanner([NeedTool("x"), CallTool("word_count", {}), CallTool("word_count", {}), Finish("ok")])
    o, _, _ = build(tmp_path, inputs, planner, ScriptedForger())
    results = iter([ok_result(exit_code=2, stderr="bad"), ok_result(exit_code=None, timed_out=True)])
    monkeypatch.setattr(orch_mod, "run_in_sandbox", lambda p, c: next(results))
    o.run("t")
    assert not planner.seen_history[2][-1].ok and "bad" in planner.seen_history[2][-1].text
    assert not planner.seen_history[3][-1].ok and "timed out" in planner.seen_history[3][-1].text


def test_oversized_args_and_output_are_bounded(tmp_path: Path, inputs: Path, monkeypatch, sandbox_calls) -> None:
    planner = ScriptedPlanner([NeedTool("x"), CallTool("word_count", {"a": "x" * 100_000}), CallTool("word_count", {}), Finish("ok")])
    o, _, _ = build(tmp_path, inputs, planner, ScriptedForger())
    monkeypatch.setattr(orch_mod, "run_in_sandbox", lambda p, c: ok_result(stdout="y" * 50_000))
    o.run("t")
    assert not planner.seen_history[2][-1].ok and "too large" in planner.seen_history[2][-1].text
    assert len(planner.seen_history[3][-1].text) <= 5000


def test_non_serializable_args_are_rejected(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    planner = ScriptedPlanner([NeedTool("x"), CallTool("word_count", {"a": object()}), Finish("ok")])
    o, _, _ = build(tmp_path, inputs, planner, ScriptedForger())
    o.run("t")
    assert not planner.seen_history[2][-1].ok



# --- forger-written tests ---------------------------------------------------------------------------

from forge.staging import cases as cases_mod  # noqa: E402
from forge.staging.cases import StagingCase  # noqa: E402

OWN_CASE = StagingCase(args={"path": "/inputs/a.txt"}, expect="42", files={"a.txt": "a b c\n"})


class ForgerWithTests(ScriptedForger):
    def __init__(self, tests: tuple[StagingCase, ...]) -> None:
        super().__init__()
        self.tests = tests

    def forge(self, need: str, workdir: Path) -> ToolDraft:
        return replace(super().forge(need, workdir), tests=self.tests)


class CaseSandbox:
    """Stands in for the sandbox the cases run in. `stdout` is what every case run prints."""

    def __init__(self) -> None:
        self.calls: list[tuple[SandboxPolicy, list[str]]] = []
        self.stdout = "42\n"

    def __call__(self, policy: SandboxPolicy, command: list[str]) -> SandboxResult:
        self.calls.append((policy, list(command)))
        return ok_result(stdout=self.stdout)


@pytest.fixture
def case_sandbox(monkeypatch: pytest.MonkeyPatch) -> CaseSandbox:
    fake = CaseSandbox()
    monkeypatch.setattr(cases_mod, "run_in_sandbox", fake)
    return fake


def build_with_tests(tmp_path: Path, inputs: Path, planner, forger, *, asked: list[str], **extra) -> tuple[Orchestrator, Registry]:
    reg = Registry(tmp_path / "registry")

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return "yes"

    o = Orchestrator(
        registry=reg, planner=planner, forger=forger, image=DIGEST, inputs_dir=inputs,
        workdir=tmp_path / "work", ask=ask, show=lambda _t: None, **extra,
    )
    return o, reg


def test_a_draft_with_passing_tests_registers_and_reports_them(tmp_path: Path, inputs: Path, sandbox_calls, case_sandbox) -> None:
    planner = ScriptedPlanner([NeedTool("count words"), Finish("done")])
    o, reg = build_with_tests(tmp_path, inputs, planner, ForgerWithTests((OWN_CASE, OWN_CASE)), asked=[])
    r = o.run("t")
    assert r.forged == ("word_count",)
    assert "2 own tests passed" in r.history[0].text
    # each case ran in its own sandbox on its own fixtures, never on the real inputs
    assert len(case_sandbox.calls) == 2
    assert all(policy.inputs_dir != inputs for policy, _ in case_sandbox.calls)
    assert case_sandbox.calls[0][1] == ["python", "/app/tool.py", json.dumps(OWN_CASE.args)]


def test_a_failing_test_stops_the_tool_before_any_human_is_asked(tmp_path: Path, inputs: Path, sandbox_calls, case_sandbox) -> None:
    case_sandbox.stdout = "7\n"
    planner = ScriptedPlanner([NeedTool("count words"), Finish("gave up")])
    asked: list[str] = []
    o, reg = build_with_tests(tmp_path, inputs, planner, ForgerWithTests((OWN_CASE,)), asked=asked)
    r = o.run("t")
    assert r.forged == ()
    assert r.history[0].text.startswith("staging failed") and "staging-test-failed" in r.history[0].text
    assert asked == []
    assert reg.list_tools() == []


def test_tests_are_required_only_when_asked(tmp_path: Path, inputs: Path, sandbox_calls, case_sandbox) -> None:
    planner = ScriptedPlanner([NeedTool("count words"), Finish("x")])
    o, reg = build_with_tests(tmp_path, inputs, planner, ScriptedForger(), asked=[], require_tests=True)
    r = o.run("t")
    assert r.forged == ()
    assert r.history[0].text == "staging failed: the forger wrote no tests"
    assert sandbox_calls == [] and case_sandbox.calls == []  # refused before anything ran


def test_drafts_without_tests_still_register_by_default(tmp_path: Path, inputs: Path, sandbox_calls, case_sandbox) -> None:
    planner = ScriptedPlanner([NeedTool("count words"), Finish("x")])
    o, reg = build_with_tests(tmp_path, inputs, planner, ScriptedForger(), asked=[])
    r = o.run("t")
    assert r.forged == ("word_count",)
    assert r.history[0].text == "registered word_count v1"


# --- staging traces ----------------------------------------------------------------------------------


def test_traces_are_kept_per_tool_and_shown_to_the_reviewer(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    planner = ScriptedPlanner([NeedTool("count words"), Finish("done")])
    o, reg, shown = build(tmp_path, inputs, planner, ScriptedForger())
    o.run("t")
    assert [(t.tool, t.label) for t in o.traces] == [("word_count", "main")]
    assert o.traces[0].stdout == "42\n" and o.traces[0].exit_code == 0
    trace_lines = [line for line in shown if line.startswith("Staging trace:")]
    assert len(trace_lines) == 1 and "main run exit 0" in trace_lines[0]
    # the summary is shown before the approval prompt reads the diff
    assert shown.index(trace_lines[0]) < len(shown) - 1


def test_a_stopped_attempt_still_leaves_its_trace(tmp_path: Path, inputs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stager, "run_in_sandbox", lambda policy, command: ok_result(exit_code=1, stdout="", stderr="Traceback: boom"))
    planner = ScriptedPlanner([NeedTool("count words"), Finish("gave up")])
    o, reg, shown = build(tmp_path, inputs, planner, ScriptedForger())
    r = o.run("t")
    assert r.forged == ()
    assert [t.stderr for t in o.traces] == ["Traceback: boom"]
    assert not any(line.startswith("Staging trace:") for line in shown)  # nobody is asked about a failed tool


def test_traces_are_not_kept_when_checks_stop_the_tool_first(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    planner = ScriptedPlanner([NeedTool("x"), Finish("gave up")])
    o, reg, _ = build(tmp_path, inputs, planner, ScriptedForger(body="import socket\n"))
    o.run("t")
    assert o.traces == [] and sandbox_calls == []


# --- capability manifest -------------------------------------------------------------------------------

import json as _json  # noqa: E402

MANIFEST_NOTES = _json.dumps({"reads": ["notes.txt"]})


class ForgerWithManifest(ScriptedForger):
    def __init__(self, manifest: str | None, **kwargs: str) -> None:
        super().__init__(**kwargs)
        self.manifest = manifest

    def forge(self, need: str, workdir: Path) -> ToolDraft:
        draft = super().forge(need, workdir)
        if self.manifest is not None:
            (draft.tool_dir / "manifest.json").write_text(self.manifest)
        return draft


@pytest.fixture
def two_inputs(tmp_path: Path) -> Path:
    d = tmp_path / "inputs"
    d.mkdir()
    (d / "notes.txt").write_text("one two\n")
    (d / "secret.txt").write_text("TOP SECRET\n")
    return d


class Mounts:
    """Records which files the sandbox could see on every run, and answers like a working tool."""

    def __init__(self) -> None:
        self.runs: list[dict] = []

    def __call__(self, policy: SandboxPolicy, command: list[str]) -> SandboxResult:
        mounted = policy.inputs_dir
        self.runs.append({"path": mounted, "files": sorted(p.name for p in mounted.rglob("*") if p.is_file()), "command": list(command)})
        return ok_result()


@pytest.fixture
def mounts(monkeypatch: pytest.MonkeyPatch) -> Mounts:
    fake = Mounts()
    monkeypatch.setattr(stager, "run_in_sandbox", fake)
    monkeypatch.setattr(orch_mod, "run_in_sandbox", fake)
    return fake


def test_staging_and_later_calls_see_only_the_declared_files(tmp_path: Path, two_inputs: Path, mounts: Mounts) -> None:
    planner = ScriptedPlanner([NeedTool("count words"), CallTool("word_count", {}), Finish("ok")])
    o, reg, _ = build(tmp_path, two_inputs, planner, ForgerWithManifest(MANIFEST_NOTES))
    r = o.run("t")
    assert r.forged == ("word_count",)
    assert [m["files"] for m in mounts.runs] == [["notes.txt"], ["notes.txt"]]  # staging run, then the real call
    assert all(m["path"] != two_inputs for m in mounts.runs)
    assert all(not m["path"].exists() for m in mounts.runs)  # views are cleaned up


def test_without_a_manifest_behaviour_is_unchanged(tmp_path: Path, two_inputs: Path, mounts: Mounts) -> None:
    planner = ScriptedPlanner([NeedTool("x"), CallTool("word_count", {}), Finish("ok")])
    o, reg, _ = build(tmp_path, two_inputs, planner, ForgerWithManifest(None))
    o.run("t")
    assert [m["files"] for m in mounts.runs] == [["notes.txt", "secret.txt"]] * 2
    assert all(m["path"] == two_inputs for m in mounts.runs)


def test_a_manifest_that_lists_a_domain_is_stopped_in_checks_and_nothing_runs(tmp_path: Path, two_inputs: Path, mounts: Mounts) -> None:
    bad = _json.dumps({"reads": ["notes.txt"], "network": ["evil.example"]})
    planner = ScriptedPlanner([NeedTool("x"), Finish("gave up")])
    o, reg, _ = build(tmp_path, two_inputs, planner, ForgerWithManifest(bad))
    r = o.run("t")
    assert r.forged == ()
    assert r.history[0].text.startswith("checks failed: manifest-invalid") and "no network" in r.history[0].text
    assert mounts.runs == [] and reg.list_tools() == []


@pytest.mark.parametrize("text", ["not json", '{"reads": ["../secret.txt"]}', '{"reads": ["/etc/passwd"]}', '{"reads": "all"}'])
def test_other_bad_manifests_are_stopped_in_checks(tmp_path: Path, two_inputs: Path, mounts: Mounts, text: str) -> None:
    planner = ScriptedPlanner([NeedTool("x"), Finish("gave up")])
    o, reg, _ = build(tmp_path, two_inputs, planner, ForgerWithManifest(text))
    r = o.run("t")
    assert r.history[0].text.startswith("checks failed: manifest-invalid")
    assert mounts.runs == []


def test_a_symlinked_manifest_is_stopped_in_checks(tmp_path: Path, two_inputs: Path, mounts: Mounts) -> None:
    class Linker(ScriptedForger):
        def forge(self, need: str, workdir: Path) -> ToolDraft:
            draft = super().forge(need, workdir)
            (draft.tool_dir / "manifest.json").symlink_to("tool.py")
            return draft

    planner = ScriptedPlanner([NeedTool("x"), Finish("gave up")])
    o, reg, _ = build(tmp_path, two_inputs, planner, Linker())
    r = o.run("t")
    assert r.history[0].text.startswith("checks failed: manifest-invalid")
    assert mounts.runs == []


def test_a_manifest_can_be_required(tmp_path: Path, two_inputs: Path, mounts: Mounts) -> None:
    planner = ScriptedPlanner([NeedTool("x"), Finish("gave up")])
    o, reg = build_with_tests(tmp_path, two_inputs, planner, ForgerWithManifest(None), asked=[], require_manifest=True)
    r = o.run("t")
    assert r.history[0].text == "checks failed: manifest-missing"
    assert mounts.runs == []


def test_a_required_manifest_that_is_present_passes(tmp_path: Path, two_inputs: Path, mounts: Mounts) -> None:
    planner = ScriptedPlanner([NeedTool("x"), Finish("ok")])
    o, reg = build_with_tests(tmp_path, two_inputs, planner, ForgerWithManifest(MANIFEST_NOTES), asked=[], require_manifest=True)
    assert o.run("t").forged == ("word_count",)


def test_a_declared_file_that_is_not_there_stops_staging(tmp_path: Path, two_inputs: Path, mounts: Mounts) -> None:
    planner = ScriptedPlanner([NeedTool("x"), Finish("gave up")])
    o, reg, _ = build(tmp_path, two_inputs, planner, ForgerWithManifest(_json.dumps({"reads": ["ghost.txt"]})))
    r = o.run("t")
    assert r.history[0].text.startswith("staging failed") and "staging-manifest-missing-file" in r.history[0].text
    assert reg.list_tools() == []


def test_an_input_the_view_cannot_safely_copy_stops_staging(tmp_path: Path, two_inputs: Path, mounts: Mounts) -> None:
    (two_inputs / "link.txt").symlink_to(tmp_path / "elsewhere.txt")
    planner = ScriptedPlanner([NeedTool("x"), Finish("gave up")])
    o, reg, _ = build(tmp_path, two_inputs, planner, ForgerWithManifest(_json.dumps({"reads": ["link.txt"]})))
    r = o.run("t")
    assert r.history[0].text.startswith("staging could not start")
    assert mounts.runs == []


def test_the_reviewer_sees_the_manifest_before_the_prompt(tmp_path: Path, two_inputs: Path, mounts: Mounts) -> None:
    planner = ScriptedPlanner([NeedTool("x"), Finish("ok")])
    o, reg, shown = build(tmp_path, two_inputs, planner, ForgerWithManifest(MANIFEST_NOTES))
    o.run("t")
    manifest_lines = [line for line in shown if line.startswith("Manifest:")]
    assert manifest_lines == ["Manifest: reads notes.txt; network none"]
    assert shown.index(manifest_lines[0]) < next(i for i, line in enumerate(shown) if line.startswith("Staging trace:"))


def test_the_manifest_is_part_of_the_diff_the_human_approves(tmp_path: Path, two_inputs: Path, mounts: Mounts) -> None:
    planner = ScriptedPlanner([NeedTool("x"), Finish("ok")])
    o, reg, shown = build(tmp_path, two_inputs, planner, ForgerWithManifest(MANIFEST_NOTES))
    o.run("t")
    assert any("manifest.json" in line and "notes.txt" in line for line in shown) or any("manifest.json" in line for line in shown)
    assert any('"reads"' in line for line in "\n".join(shown).splitlines())


def test_editing_the_manifest_in_the_registry_copy_is_caught_by_the_hash(tmp_path: Path, two_inputs: Path, mounts: Mounts) -> None:
    class Planner(ScriptedPlanner):
        def next_action(self, task, tools, history):  # type: ignore[no-untyped-def]
            action = super().next_action(task, tools, history)
            if isinstance(action, CallTool):  # widen the declaration after approval
                (reg.get("word_count").path / "manifest.json").write_text(_json.dumps({"reads": ["notes.txt", "secret.txt"]}))
            return action

    planner = Planner([NeedTool("x"), CallTool("word_count", {}), Finish("done")])
    o, reg, _ = build(tmp_path, two_inputs, planner, ForgerWithManifest(MANIFEST_NOTES))
    r = o.run("t")
    call = [h for h in r.history if h.kind == "tool"][0]
    assert not call.ok and "failed hash verification" in call.text
    assert len(mounts.runs) == 1  # only the staging run; the widened call never reached the sandbox


def test_a_tool_registered_without_a_manifest_is_refused_when_one_is_required(tmp_path: Path, two_inputs: Path, mounts: Mounts) -> None:
    reg = Registry(tmp_path / "registry")
    legacy, _, _ = build(tmp_path, two_inputs, ScriptedPlanner([NeedTool("x"), Finish("ok")]), ForgerWithManifest(None))
    legacy._registry = reg  # type: ignore[attr-defined]
    legacy.run("t")
    strict = Orchestrator(
        registry=reg, planner=ScriptedPlanner([CallTool("word_count", {}), Finish("done")]), forger=ForgerWithManifest(None),
        image=DIGEST, inputs_dir=two_inputs, workdir=tmp_path / "w2", ask=lambda _p: "yes", show=lambda _t: None, require_manifest=True,
    )
    r = strict.run("t")
    call = [h for h in r.history if h.kind == "tool"][0]
    assert not call.ok and "no manifest" in call.text


def test_call_tool_is_the_same_path_a_planner_call_takes(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    o, reg, _ = build(tmp_path, inputs, ScriptedPlanner([NeedTool("count words"), Finish("done")]), ScriptedForger())
    o.run("t")
    sandbox_calls.clear()
    obs = o.call_tool("word_count", {"file": "log.txt"})
    assert obs.ok and obs.text == "42\n"
    assert sandbox_calls[0][1] == ["python", "/app/tool.py", json.dumps({"file": "log.txt"})]
    assert sandbox_calls[0][0].tool_dir == reg.get("word_count").path


def test_call_tool_refuses_unknown_tools_and_bad_names(tmp_path: Path, inputs: Path, sandbox_calls) -> None:
    o, _, _ = build(tmp_path, inputs, ScriptedPlanner([]), ScriptedForger())
    assert not o.call_tool("missing", {}).ok
    assert not o.call_tool("../evil", {}).ok
    assert sandbox_calls == []
