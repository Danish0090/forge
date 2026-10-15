"""Capability manifest in the benchmark: the oracle declares what it reads and --require-manifest reaches every run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bench_helpers import make_fake_orchestrator, worked_out
from forge.bench import benchmark
from forge.bench.__main__ import main
from forge.bench.tasks import TASKS

DIGEST = "python@sha256:" + "a" * 64
TASK = next(t for t in TASKS if t.id == "word-count")


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_the_oracle_declares_exactly_the_files_of_its_task(task, tmp_path: Path) -> None:
    draft = benchmark.OracleForger(task).forge("need", tmp_path)
    manifest = json.loads((draft.tool_dir / "manifest.json").read_text())
    assert manifest == {"reads": sorted(task.files), "network": []}


def oracle_agents(task, files):  # type: ignore[no-untyped-def]
    return benchmark.OraclePlanner(task), benchmark.OracleForger(task)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    cls = make_fake_orchestrator()
    cls.answer_for = staticmethod(lambda files, prompt: worked_out("word-count", files))
    monkeypatch.setattr(benchmark, "Orchestrator", cls)
    return cls


def run(tmp_path: Path, **kw: object) -> benchmark.TaskResult:
    return benchmark.run_task(TASK, oracle_agents, image=DIGEST, workdir=tmp_path, ask=lambda _p: "yes", show=lambda _t: None, **kw)  # type: ignore[arg-type]


def test_require_manifest_reaches_the_main_hidden_and_survival_runs(tmp_path: Path, fake) -> None:
    run(tmp_path, require_manifest=True)
    assert len(fake.built) == 3  # main, hidden, survival
    assert all(kwargs["require_manifest"] is True for kwargs in fake.built)


def test_it_is_off_by_default(tmp_path: Path, fake) -> None:
    run(tmp_path)
    assert all(kwargs["require_manifest"] is False for kwargs in fake.built)


def test_drafts_record_the_manifest_text(tmp_path: Path) -> None:
    from forge.orchestrator import ToolDraft  # noqa: F401

    rec = benchmark._RecordingForger(benchmark.OracleForger(TASK))
    rec.forge("need", tmp_path)
    assert json.loads(rec.drafts[0].manifest) == {"reads": ["notes.txt"], "network": []}


def test_a_draft_without_a_manifest_records_an_empty_string(tmp_path: Path) -> None:
    class Bare(benchmark.OracleForger):
        def forge(self, need: str, workdir: Path):  # type: ignore[no-untyped-def]
            draft = super().forge(need, workdir)
            (draft.tool_dir / "manifest.json").unlink()
            return draft

    rec = benchmark._RecordingForger(Bare(TASK))
    rec.forge("need", tmp_path)
    assert rec.drafts[0].manifest == ""


def test_cli_passes_require_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_SANDBOX_IMAGE", DIGEST)
    monkeypatch.setattr("forge.bench.__main__.docker_ready", lambda: True)
    seen: dict[str, object] = {}
    monkeypatch.setattr(benchmark, "run_benchmark", lambda factory, **kw: seen.update(kw) or [])
    main(["bench", "--model", "oracle", "--tasks", "word-count", "--require-manifest"])
    assert seen["require_manifest"] is True
    main(["bench", "--model", "oracle", "--tasks", "word-count"])
    assert seen["require_manifest"] is False


@pytest.mark.docker
def test_oracle_gets_every_task_right_when_a_manifest_is_required(sandbox_image: str) -> None:
    results = benchmark.run_benchmark(benchmark.oracle_factory, image=sandbox_image, ask=lambda _p: "yes", show=lambda _t: None, require_manifest=True)
    assert [(r.task_id, r.failures) for r in results if not r.correct] == []
    assert all(r.forge_attempts == 1 for r in results)
    assert all(r.hidden_passed == r.hidden_total and r.survival_passed == r.survival_total for r in results)


# --- drafts keep their test cases, so a failed test can be judged afterwards -------------------------------


def test_drafts_record_the_full_test_cases(tmp_path: Path) -> None:
    rec = benchmark._RecordingForger(benchmark.OracleForger(TASK))
    rec.forge("need", tmp_path)
    (case,) = rec.drafts[0].tests
    assert case["files"] == TASK.variants[0].files
    assert case["args"] == {"path": "/inputs/notes.txt"}
    assert case["expect"] == TASK.variants[0].expected
    json.dumps(case)  # plain data, so it serialises into the --out report


def test_a_draft_with_no_tests_records_an_empty_list(tmp_path: Path) -> None:
    class NoTests(benchmark.OracleForger):
        def forge(self, need: str, workdir: Path):  # type: ignore[no-untyped-def]
            import dataclasses

            return dataclasses.replace(super().forge(need, workdir), tests=())

    rec = benchmark._RecordingForger(NoTests(TASK))
    rec.forge("need", tmp_path)
    assert rec.drafts[0].tests == ()


# --- --max-tokens: forger replies now carry code, tests and a manifest, so the cap is adjustable -----------------


def test_model_agents_pass_max_tokens_to_the_router() -> None:
    agents = benchmark.ModelAgents("nemotron", max_tokens=9000)
    assert agents.router._max_tokens == 9000  # noqa: SLF001


def test_the_default_cap_is_unchanged() -> None:
    assert benchmark.ModelAgents("nemotron").router._max_tokens == 4000  # noqa: SLF001


def test_cli_passes_max_tokens_and_validates_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_SANDBOX_IMAGE", DIGEST)
    monkeypatch.setattr("forge.bench.__main__.docker_ready", lambda: True)
    seen: dict[str, object] = {}

    def fake_factory(model: str, **kw: object):  # type: ignore[no-untyped-def]
        seen.update(kw)
        return benchmark.oracle_factory

    monkeypatch.setattr(benchmark, "model_factory", fake_factory)
    monkeypatch.setattr(benchmark, "run_benchmark", lambda factory, **kw: [])
    assert main(["bench", "--model", "nemotron", "--tasks", "word-count", "--max-tokens", "12000"]) == 0
    assert seen["max_tokens"] == 12000
    seen.clear()
    main(["bench", "--model", "nemotron", "--tasks", "word-count"])
    assert seen["max_tokens"] is None
    assert main(["bench", "--model", "nemotron", "--tasks", "word-count", "--max-tokens", "10"]) == 2
    assert main(["bench", "--model", "nemotron", "--tasks", "word-count", "--max-tokens", "999999"]) == 2
