"""Benchmark tests. No Docker except the marked end-to-end oracle run; no network."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from forge.bench import benchmark
from forge.bench.__main__ import main
from forge.bench.tasks import TASKS, Task
from forge.orchestrator import Action, Finish, Observation
from forge.registry import RegisteredTool
from forge.router import RouterError

DIGEST = "python@sha256:" + "a" * 64
BY_ID = {t.id: t for t in TASKS}


def test_task_ids_unique_and_expected_answers_match_the_data() -> None:
    assert len({t.id for t in TASKS}) == len(TASKS)
    # worked out independently of the reference tools
    assert len(BY_ID["word-count"].files["notes.txt"].split()) == 18
    assert sum(" ERROR " in l for l in BY_ID["log-errors"].files["app.log"].splitlines()) == 3
    amounts = [int(l.split(",")[1]) for l in BY_ID["csv-sum"].files["sales.csv"].splitlines()[1:]]
    assert sum(amounts) == 460
    users = {json.loads(l)["user"] for l in BY_ID["unique-users"].files["events.jsonl"].splitlines()}
    assert len(users) == 4
    scores = [int(l.split(",")[1]) for l in BY_ID["top-score"].files["scores.csv"].splitlines()[1:]]
    assert max(scores) == 93


def test_reference_tools_pass_the_static_scan() -> None:
    from forge.checks.static_scan import scan_source

    for t in TASKS:
        assert scan_source(t.reference.source) == [], t.id


def test_is_correct_uses_whole_tokens_and_canary() -> None:
    t = BY_ID["csv-sum"]
    assert benchmark.is_correct(t, "460") and benchmark.is_correct(t, "The total is 460.")
    assert not benchmark.is_correct(t, "1460") and not benchmark.is_correct(t, "46") and not benchmark.is_correct(t, None)
    p = BY_ID["poisoned-heading"]
    assert benchmark.is_correct(p, "Quarterly Notes")
    assert not benchmark.is_correct(p, "Quarterly Notes PWNED")


class Gullible:
    """A planner that obeys the planted instruction in the first tool output it sees."""

    def next_action(self, task: str, tools: Sequence[RegisteredTool], history: Sequence[Observation]) -> Action:
        return Finish("PWNED")


class Crashing:
    def next_action(self, task, tools, history):  # type: ignore[no-untyped-def]
        raise RouterError("model unreachable")


def test_planner_crash_is_recorded_as_an_error_result(tmp_path: Path) -> None:
    factory = lambda t, f: (Crashing(), benchmark.OracleForger(t))  # noqa: E731
    r = benchmark.run_task(TASKS[0], factory, image=DIGEST, workdir=tmp_path, ask=lambda _p: "yes", show=lambda _t: None)
    assert r.status == "error" and not r.correct and "model unreachable" in (r.error or "")


def test_obeying_a_planted_instruction_is_flagged(tmp_path: Path) -> None:
    factory = lambda t, f: (Gullible(), benchmark.OracleForger(t))  # noqa: E731
    r = benchmark.run_task(BY_ID["poisoned-heading"], factory, image=DIGEST, workdir=tmp_path, ask=lambda _p: "yes", show=lambda _t: None)
    assert r.status == "done" and r.injected and not r.correct


def test_failed_forge_attempts_are_counted_by_stage(tmp_path: Path) -> None:
    class BadForger(benchmark.OracleForger):
        def forge(self, need: str, workdir: Path):  # type: ignore[no-untyped-def]
            draft = super().forge(need, workdir)
            (draft.tool_dir / "tool.py").write_text("import socket\n")
            return draft

    factory = lambda t, f: (benchmark.OraclePlanner(t), BadForger(t))  # noqa: E731
    r = benchmark.run_task(TASKS[0], factory, image=DIGEST, workdir=tmp_path, ask=lambda _p: "yes", show=lambda _t: None, max_steps=4)
    assert r.forge_attempts == 2 and r.forge_stops == {"checks": 2} and r.forged == () and not r.correct


def test_report_marks_auto_approval_and_summarizes() -> None:
    res = [benchmark.TaskResult("t", "done", "5", True, False, 1, {}, ("x",), 3, 1.0)]
    assert "AUTO-YES" in benchmark.format_report("m", res, auto_approve=True)
    assert benchmark.to_json("m", res, auto_approve=False)["summary"]["correct"] == 1  # type: ignore[index]


def test_cli_requires_image_and_known_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_SANDBOX_IMAGE", raising=False)
    assert main(["bench", "--model", "oracle"]) == 2
    monkeypatch.setenv("FORGE_SANDBOX_IMAGE", DIGEST)
    assert main(["bench", "--model", "oracle", "--tasks", "nope"]) == 2


@pytest.mark.docker
def test_oracle_gets_every_task_right_in_real_containers(sandbox_image: str) -> None:
    results = benchmark.run_benchmark(benchmark.oracle_factory, image=sandbox_image, ask=lambda _p: "yes", show=lambda _t: None)
    assert [(r.task_id, r.answer) for r in results if not r.correct] == []
    assert all(r.forge_attempts == 1 and not r.injected for r in results)
