"""Hidden test set: after a task, a registered tool is asked the same question over different files.

The answers were worked out by hand here, independently of the reference tools. A tool that only
passes its own tests, or hard-codes the first answer, fails these. Unit tests fake the orchestrator;
the marked Docker test runs the oracle's reference tools over every variant in real containers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.bench import benchmark
from forge.bench.__main__ import main
from forge.bench.tasks import TASKS, Task, Variant
from bench_helpers import make_fake_orchestrator, worked_out
from forge.orchestrator.orchestrator import RunResult

DIGEST = "python@sha256:" + "a" * 64
BY_ID = {t.id: t for t in TASKS}
WITH_VARIANTS = [t for t in TASKS if t.variants]


def test_the_easy_and_hard_tasks_without_planted_text_have_a_variant() -> None:
    assert {t.id for t in WITH_VARIANTS} == {
        "word-count", "log-errors", "csv-sum", "unique-users", "top-score",
        "join-region", "messy-ledger", "log-window", "latest-action", "date-span",
    }


@pytest.mark.parametrize("task", WITH_VARIANTS, ids=lambda t: t.id)
def test_variant_answers_match_independent_calculations(task: Task) -> None:
    for v in task.variants:
        assert worked_out(task.id, v.files) == v.expected


@pytest.mark.parametrize("task", WITH_VARIANTS, ids=lambda t: t.id)
def test_variants_use_the_same_file_names_but_a_different_answer(task: Task) -> None:
    for v in task.variants:
        assert set(v.files) == set(task.files)  # the prompt names the files, so they must keep their names
        assert v.files != task.files
        assert v.expected != task.expected  # a tool that hard-codes the first answer must fail


def test_injection_tier_and_planted_text_tasks_have_no_variants() -> None:
    assert all(not t.variants for t in TASKS if t.canary or t.tier == "injection")


# --- scoring ----------------------------------------------------------------------------------------


def test_answer_matching_is_shared_between_main_and_hidden_scoring() -> None:
    assert benchmark.answer_matches("102", "The top score is 102.")
    assert not benchmark.answer_matches("102", "1020")
    assert not benchmark.answer_matches("3", "PWNED 3", canary="PWNED")


# --- run_hidden and run_task, with the orchestrator faked ---------------------------------------------


@pytest.fixture
def fake_orchestrator(monkeypatch: pytest.MonkeyPatch):
    fake = make_fake_orchestrator()
    monkeypatch.setattr(benchmark, "Orchestrator", fake)
    return fake


def oracle_agents(task: Task, files):  # type: ignore[no-untyped-def]
    return benchmark.OraclePlanner(task), benchmark.OracleForger(task)


TASK = BY_ID["word-count"]


def test_hidden_runs_use_the_same_registry_variant_files_and_no_forging(tmp_path: Path, fake_orchestrator) -> None:
    fake_orchestrator.answer_for = staticmethod(lambda files, prompt: worked_out("word-count", files))
    registry = object()
    passed, total = benchmark.run_hidden(
        TASK, oracle_agents, registry=registry, image=DIGEST, workdir=tmp_path,  # type: ignore[arg-type]
        ask=lambda _p: "yes", show=lambda _t: None, max_steps=7, timeout_s=3.0,
    )
    assert (passed, total) == (len(TASK.variants), len(TASK.variants))
    built = fake_orchestrator.built[0]
    assert built["registry"] is registry
    assert built["max_forges"] == 0 and built["max_steps"] == 7 and built["timeout_s"] == 3.0
    with pytest.raises(Exception):
        built["forger"].forge("anything", tmp_path)  # a hidden run can never write a new tool


def test_a_wrong_hidden_answer_is_not_counted(tmp_path: Path, fake_orchestrator) -> None:
    fake_orchestrator.answer_for = staticmethod(lambda files, prompt: TASK.expected)  # the main-run answer, hard-coded
    passed, total = benchmark.run_hidden(
        TASK, oracle_agents, registry=object(), image=DIGEST, workdir=tmp_path,  # type: ignore[arg-type]
        ask=lambda _p: "yes", show=lambda _t: None, max_steps=7, timeout_s=3.0,
    )
    assert (passed, total) == (0, len(TASK.variants))


def test_a_crash_in_a_hidden_run_counts_as_a_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        def __init__(self, **kwargs: object) -> None: ...
        def run(self, prompt: str) -> RunResult:
            raise RuntimeError("planner unreachable")

    monkeypatch.setattr(benchmark, "Orchestrator", Boom)
    passed, total = benchmark.run_hidden(
        TASK, oracle_agents, registry=object(), image=DIGEST, workdir=tmp_path,  # type: ignore[arg-type]
        ask=lambda _p: "yes", show=lambda _t: None, max_steps=7, timeout_s=3.0,
    )
    assert (passed, total) == (0, len(TASK.variants))


def test_task_result_records_hidden_scores_only_when_a_tool_was_registered(tmp_path: Path, fake_orchestrator) -> None:
    fake_orchestrator.answer_for = staticmethod(lambda files, prompt: worked_out("word-count", files))
    r = benchmark.run_task(TASK, oracle_agents, image=DIGEST, workdir=tmp_path / "a", ask=lambda _p: "yes", show=lambda _t: None)
    assert (r.hidden_passed, r.hidden_total) == (len(TASK.variants), len(TASK.variants))

    fake_orchestrator.built = []
    monkey_forged = RunResult("done", "18", (), ())  # nothing registered
    fake_orchestrator.run = lambda self, prompt: monkey_forged  # type: ignore[method-assign]
    r = benchmark.run_task(TASK, oracle_agents, image=DIGEST, workdir=tmp_path / "b", ask=lambda _p: "yes", show=lambda _t: None)
    assert (r.hidden_passed, r.hidden_total) == (0, 0)


def test_hidden_can_be_switched_off(tmp_path: Path, fake_orchestrator) -> None:
    fake_orchestrator.answer_for = staticmethod(lambda files, prompt: worked_out("word-count", files))
    r = benchmark.run_task(TASK, oracle_agents, image=DIGEST, workdir=tmp_path, ask=lambda _p: "yes", show=lambda _t: None, hidden=False, survival=False)
    assert (r.hidden_passed, r.hidden_total) == (0, 0)
    assert len(fake_orchestrator.built) == 1


def test_require_tests_reaches_the_orchestrator(tmp_path: Path, fake_orchestrator) -> None:
    benchmark.run_task(TASK, oracle_agents, image=DIGEST, workdir=tmp_path, ask=lambda _p: "yes", show=lambda _t: None, require_tests=True, hidden=False)
    assert fake_orchestrator.built[0]["require_tests"] is True


# --- reporting --------------------------------------------------------------------------------------


def result(**kw: object) -> benchmark.TaskResult:
    base = dict(task_id="t", status="done", answer="5", correct=True, injected=False, forge_attempts=1, forge_stops={}, forged=("x",), steps=3, seconds=1.0)
    base.update(kw)
    return benchmark.TaskResult(**base)  # type: ignore[arg-type]


def test_summary_counts_hidden_and_own_tests() -> None:
    res = [
        result(hidden_passed=1, hidden_total=1, own_tests=2),
        result(task_id="u", correct=False, hidden_passed=0, hidden_total=1, own_tests=3),
        result(task_id="v", forged=(), hidden_total=0),
    ]
    s = benchmark.summarize(res)
    assert s["hidden_passed"] == 1 and s["hidden_total"] == 2
    assert s["tools_with_own_tests"] == 2
    assert s["correct_and_hidden"] == 1  # right on the main task and on every hidden variant


def test_report_mentions_hidden_results_only_when_there_were_any() -> None:
    plain = benchmark.format_report("m", [result()], auto_approve=False)
    assert "hidden" not in plain
    with_hidden = benchmark.format_report("m", [result(hidden_passed=1, hidden_total=2)], auto_approve=False)
    assert "hidden test set: 1 of 2" in with_hidden


def test_json_keeps_the_new_fields() -> None:
    out = benchmark.to_json("m", [result(hidden_passed=1, hidden_total=1, own_tests=2)], auto_approve=False)
    first = out["results"][0]  # type: ignore[index]
    assert (first["hidden_passed"], first["hidden_total"], first["own_tests"]) == (1, 1, 2)


def test_own_tests_are_read_from_the_registered_observation() -> None:
    from forge.orchestrator import Observation

    history = [Observation("forge", "need", True, "registered word_count v1 (3 own tests passed)"), Observation("forge", "n", False, "staging failed: x")]
    assert benchmark.own_tests_passed(history) == 3
    assert benchmark.own_tests_passed([Observation("forge", "n", True, "registered a v1")]) == 0


# --- command line -----------------------------------------------------------------------------------


def test_cli_passes_require_tests_and_no_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_SANDBOX_IMAGE", DIGEST)
    monkeypatch.setattr("forge.bench.__main__.docker_ready", lambda: True)
    seen: dict[str, object] = {}

    def fake_run(factory, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return []

    monkeypatch.setattr(benchmark, "run_benchmark", fake_run)
    main(["bench", "--model", "oracle", "--tasks", "word-count", "--require-tests", "--no-hidden"])
    assert seen["require_tests"] is True and seen["hidden"] is False
    seen.clear()
    main(["bench", "--model", "oracle", "--tasks", "word-count"])
    assert seen["require_tests"] is False and seen["hidden"] is True


# --- the oracle over every variant, in real containers ----------------------------------------------


@pytest.mark.docker
def test_reference_tools_answer_every_hidden_variant_in_real_containers(sandbox_image: str) -> None:
    results = benchmark.run_benchmark(
        benchmark.oracle_factory, image=sandbox_image, tasks=WITH_VARIANTS, ask=lambda _p: "yes", show=lambda _t: None
    )
    assert [(r.task_id, r.hidden_passed, r.hidden_total) for r in results if r.hidden_passed != r.hidden_total] == []
    assert all(r.hidden_total == 1 and r.correct for r in results)


@pytest.mark.docker
def test_oracle_with_its_own_tests_passes_when_tests_are_required(sandbox_image: str) -> None:
    results = benchmark.run_benchmark(
        benchmark.oracle_factory, image=sandbox_image, tasks=WITH_VARIANTS, ask=lambda _p: "yes", show=lambda _t: None, require_tests=True
    )
    assert [(r.task_id, r.failures) for r in results if not r.correct] == []
    assert all(r.own_tests >= 1 for r in results)
