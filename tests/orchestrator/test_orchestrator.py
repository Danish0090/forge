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
