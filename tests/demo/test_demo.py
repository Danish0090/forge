"""Demo tests. No Docker, no network: the sandbox is faked and models are never called."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from forge import demo
from forge.checks import run_checks
from forge.checks.hash_pin import tree_hash
from forge.orchestrator import orchestrator as orch_mod
from forge.router.agents import FORGER_SYSTEM
from forge.sandbox.runner import SandboxResult
from forge.staging import stager

DIGEST = "python@sha256:" + "a" * 64


def test_synthetic_inputs_are_written(tmp_path: Path) -> None:
    d = demo.write_inputs(tmp_path)
    assert (d / "notes.txt").read_text().split()


def test_scripted_bad_tool_fails_checks_and_good_tool_passes(tmp_path: Path) -> None:
    forger = demo.ScriptedForger()
    bad = forger.forge("anything", tmp_path)
    good = forger.forge("anything", tmp_path)
    assert not run_checks(bad.tool_dir, bad.description, tree_hash(bad.tool_dir)).passed
    assert run_checks(good.tool_dir, good.description, tree_hash(good.tool_dir)).passed


def test_prompts_make_argument_names_part_of_the_tool_description() -> None:
    from forge.router.agents import PLANNER_SYSTEM

    assert "argument names" in FORGER_SYSTEM and "argument names" in PLANNER_SYSTEM


def test_forger_prompt_tells_models_how_to_read_args_without_sys() -> None:
    assert "argparse" in FORGER_SYSTEM and "sys" in FORGER_SYSTEM


def test_scripted_run_rejects_bad_then_registers_good_and_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(policy, command):  # type: ignore[no-untyped-def]
        return SandboxResult("forge-x", 0, "7\n", "", False, False, False)

    monkeypatch.setattr(stager, "run_in_sandbox", fake)
    monkeypatch.setattr(orch_mod, "run_in_sandbox", fake)
    result = demo.run(
        model="scripted", image=DIGEST, workdir=tmp_path, ask=lambda _p: "yes", show=lambda _s: None
    )
    assert result.status == "done" and result.answer == "7"
    assert result.forged == ("word_count",)
    assert [o.ok for o in result.history if o.kind == "forge"] == [False, True]


def test_main_requires_a_pinned_image(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("FORGE_SANDBOX_IMAGE", raising=False)
    assert demo.main(["--model", "scripted"]) == 2
    assert "FORGE_SANDBOX_IMAGE" in capsys.readouterr().err


def test_main_reports_unknown_model_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FORGE_SANDBOX_IMAGE", DIGEST)
    assert demo.main(["--model", "nope"]) == 2
    assert "unknown model" in capsys.readouterr().err
