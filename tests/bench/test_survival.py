"""Survival: after a task, does the registered tool still answer right when the input format drifts?

For these file-reading tools the "API" is the input format. A drift changes things the task prompt
does not promise (column order, extra columns or fields, line endings, tabs between fields) and the
right answer stays the same. It is a proxy for an API change, not a real one. Unit tests fake the
orchestrator; the marked Docker test runs the oracle's reference tools over every drift.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from bench_helpers import make_fake_orchestrator, worked_out
from forge.bench import benchmark
from forge.bench.__main__ import main
from forge.bench.tasks import TASKS, Task
from forge.staging.trace import make_trace
from forge.sandbox.runner import SandboxResult

DIGEST = "python@sha256:" + "a" * 64
BY_ID = {t.id: t for t in TASKS}
WITH_DRIFTS = [t for t in TASKS if t.drifts]
TASK = BY_ID["word-count"]


def test_every_task_with_a_hidden_variant_also_has_a_drift() -> None:
    assert {t.id for t in WITH_DRIFTS} == {t.id for t in TASKS if t.variants}
    assert all(not t.drifts for t in TASKS if t.canary or t.tier == "injection")


@pytest.mark.parametrize("task", WITH_DRIFTS, ids=lambda t: t.id)
def test_the_answer_survives_the_drift_by_an_independent_calculation(task: Task) -> None:
    for d in task.drifts:
        assert d.expected == task.expected  # the data means the same thing, only its shape changed
        assert worked_out(task.id, d.files) == task.expected


@pytest.mark.parametrize("task", WITH_DRIFTS, ids=lambda t: t.id)
def test_a_drift_keeps_the_file_names_but_changes_the_bytes(task: Task) -> None:
    for d in task.drifts:
        assert set(d.files) == set(task.files)  # the prompt names the files
        assert d.files != task.files
        assert all(d.files[name] != task.files[name] for name in d.files)  # every file really changed shape


# --- run_task with the orchestrator faked ------------------------------------------------------------


def oracle_agents(task: Task, files):  # type: ignore[no-untyped-def]
    return benchmark.OraclePlanner(task), benchmark.OracleForger(task)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    cls = make_fake_orchestrator()
    monkeypatch.setattr(benchmark, "Orchestrator", cls)
    return cls


def run(tmp_path: Path, **kw: object) -> benchmark.TaskResult:
    return benchmark.run_task(TASK, oracle_agents, image=DIGEST, workdir=tmp_path, ask=lambda _p: "yes", show=lambda _t: None, **kw)  # type: ignore[arg-type]


def is_drifted(files: dict[str, str]) -> bool:
    return files == TASK.drifts[0].files


def test_a_robust_tool_survives_the_drift(tmp_path: Path, fake) -> None:
    fake.answer_for = staticmethod(lambda files, prompt: worked_out("word-count", files))
    r = run(tmp_path)
    assert (r.survival_passed, r.survival_total) == (1, 1)


def test_a_brittle_tool_fails_the_drift(tmp_path: Path, fake) -> None:
    fake.answer_for = staticmethod(lambda files, prompt: "0" if is_drifted(files) else worked_out("word-count", files))
    r = run(tmp_path)
    assert (r.survival_passed, r.survival_total) == (0, 1)
    assert r.correct  # it was right before the drift, which is what makes the failure a survival failure


def test_survival_is_only_scored_for_tools_that_were_right_first(tmp_path: Path, fake) -> None:
    fake.answer_for = staticmethod(lambda files, prompt: "99" if files == TASK.files else worked_out("word-count", files))
    r = run(tmp_path)
    assert not r.correct
    assert (r.survival_passed, r.survival_total) == (0, 0)


def test_survival_can_be_switched_off(tmp_path: Path, fake) -> None:
    fake.answer_for = staticmethod(lambda files, prompt: worked_out("word-count", files))
    r = run(tmp_path, survival=False)
    assert (r.survival_passed, r.survival_total) == (0, 0)
    assert (r.hidden_passed, r.hidden_total) == (1, 1)  # the hidden set is separate
    assert len(fake.built) == 2  # main run and one hidden run, no drift run


def test_drift_runs_cannot_forge(tmp_path: Path, fake) -> None:
    fake.answer_for = staticmethod(lambda files, prompt: worked_out("word-count", files))
    run(tmp_path)
    drift_run = fake.built[-1]
    assert drift_run["max_forges"] == 0
    with pytest.raises(Exception):
        drift_run["forger"].forge("anything", tmp_path)


# --- traces in the task result -----------------------------------------------------------------------


def test_staging_traces_are_saved_in_the_task_result_and_capped(tmp_path: Path, fake) -> None:
    t = make_trace("main", ["python", "/app/tool.py", "{}"], SandboxResult("c", 0, "18\n", "", False, False, False), 0.2)
    fake.trace_items = [dataclasses.replace(t, tool="word_count")] * 12
    fake.answer_for = staticmethod(lambda files, prompt: worked_out("word-count", files))
    r = run(tmp_path, hidden=False, survival=False)
    assert len(r.staging_traces) == benchmark.MAX_TRACES == 8
    assert r.staging_traces[0]["tool"] == "word_count" and r.staging_traces[0]["stdout"] == "18\n"
    json.dumps(dataclasses.asdict(r))  # serialises into the --out report


# --- reporting ---------------------------------------------------------------------------------------


def result(**kw: object) -> benchmark.TaskResult:
    base = dict(task_id="t", status="done", answer="5", correct=True, injected=False, forge_attempts=1, forge_stops={}, forged=("x",), steps=3, seconds=1.0)
    base.update(kw)
    return benchmark.TaskResult(**base)  # type: ignore[arg-type]


def test_summary_counts_survival() -> None:
    res = [result(survival_passed=1, survival_total=1), result(task_id="u", survival_passed=0, survival_total=1), result(task_id="v")]
    s = benchmark.summarize(res)
    assert (s["survival_passed"], s["survival_total"]) == (1, 2)


def test_report_mentions_survival_only_when_it_was_measured() -> None:
    assert "survival" not in benchmark.format_report("m", [result()], auto_approve=False)
    text = benchmark.format_report("m", [result(survival_passed=1, survival_total=2)], auto_approve=False)
    assert "survival after input drift: 1 of 2" in text
    assert "proxy" in text  # the report says it is a simulated change, not a real API change


def test_runs_summary_adds_survival_across_runs() -> None:
    s = benchmark.summarize_runs([[result(survival_passed=1, survival_total=1)], [result(survival_passed=0, survival_total=1)]])
    assert (s["survival_passed"], s["survival_total"]) == (1, 2)


def test_cli_no_survival_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_SANDBOX_IMAGE", DIGEST)
    monkeypatch.setattr("forge.bench.__main__.docker_ready", lambda: True)
    seen: dict[str, object] = {}
    monkeypatch.setattr(benchmark, "run_benchmark", lambda factory, **kw: seen.update(kw) or [])
    main(["bench", "--model", "oracle", "--tasks", "word-count", "--no-survival"])
    assert seen["survival"] is False
    main(["bench", "--model", "oracle", "--tasks", "word-count"])
    assert seen["survival"] is True


# --- the oracle over every drift, in real containers -------------------------------------------------


@pytest.mark.docker
def test_reference_tools_survive_every_drift_in_real_containers(sandbox_image: str) -> None:
    results = benchmark.run_benchmark(benchmark.oracle_factory, image=sandbox_image, tasks=WITH_DRIFTS, ask=lambda _p: "yes", show=lambda _t: None)
    assert [(r.task_id, r.survival_passed, r.survival_total) for r in results if r.survival_passed != r.survival_total] == []
    assert all(r.survival_total == 1 for r in results)
    assert all(r.staging_traces for r in results)  # real traces came back from real containers


# A tool that reads the amount by column position is right on the original file and wrong once the
# columns move. This is what the metric exists to catch.
POSITIONAL_SUM = (
    "import argparse, csv, json\nfrom pathlib import Path\n"
    "p = argparse.ArgumentParser(); p.add_argument('payload')\n"
    "args = json.loads(p.parse_args().payload)\n"
    "rows = list(csv.reader(Path(args['path']).open()))[1:]\n"
    "print(sum(int(r[1]) for r in rows))\n"
)


class PositionalForger(benchmark.OracleForger):
    def forge(self, need: str, workdir: Path):  # type: ignore[no-untyped-def]
        draft = super().forge(need, workdir)
        (draft.tool_dir / "tool.py").write_text(POSITIONAL_SUM)
        return dataclasses.replace(draft, tests=())  # its own tests are skipped: this checks only the drift


@pytest.mark.docker
def test_a_fragile_tool_is_right_first_and_fails_the_drift(tmp_path: Path, sandbox_image: str) -> None:
    task = BY_ID["csv-sum"]
    factory = lambda t, f: (benchmark.OraclePlanner(t), PositionalForger(t))  # noqa: E731
    r = benchmark.run_task(task, factory, image=sandbox_image, workdir=tmp_path, ask=lambda _p: "yes", show=lambda _t: None)
    assert r.correct, r.failures  # right on the original file
    assert (r.survival_passed, r.survival_total) == (0, 1)  # wrong once the columns moved
    assert (r.hidden_passed, r.hidden_total) == (1, 1)  # the hidden variant keeps the original column order, so it cannot tell
