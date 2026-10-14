"""Unit tests for the staging stage. The sandbox is faked, so no Docker daemon is needed."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from forge.sandbox.policy import SandboxPolicy
from forge.sandbox.runner import SandboxResult
from forge.staging import stager
from forge.staging.stager import run_staging

DIGEST = "python@sha256:" + "a" * 64


def ok_result(**overrides: object) -> SandboxResult:
    base = SandboxResult("forge-x", 0, "done\n", "", False, False, False)
    return replace(base, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    tool = tmp_path / "tool"
    tool.mkdir()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "log.txt").write_text("line one\n")
    return tool, inputs


@pytest.fixture
def fake_sandbox(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[SandboxPolicy, list[str]]] = []
    state = {"result": ok_result(), "side_effect": None}

    def fake(policy: SandboxPolicy, command: list[str]) -> SandboxResult:
        calls.append((policy, list(command)))
        if state["side_effect"]:
            state["side_effect"]()
        return state["result"]  # type: ignore[return-value]

    monkeypatch.setattr(stager, "run_in_sandbox", fake)
    return calls, state


def test_clean_run_passes(dirs, fake_sandbox) -> None:
    tool, inputs = dirs
    report = run_staging(DIGEST, tool, inputs, ["python", "/app/tool.py"])
    assert report.passed
    assert report.findings == ()
    assert report.result.stdout == "done\n"


def test_inputs_are_mounted_through_the_policy(dirs, fake_sandbox) -> None:
    tool, inputs = dirs
    calls, _ = fake_sandbox
    run_staging(DIGEST, tool, inputs, ["python", "/app/tool.py"])
    policy, command = calls[0]
    assert policy.inputs_dir == inputs
    assert policy.tool_dir == tool
    assert command == ["python", "/app/tool.py"]


def test_nonzero_exit_fails(dirs, fake_sandbox) -> None:
    tool, inputs = dirs
    _, state = fake_sandbox
    state["result"] = ok_result(exit_code=2)
    report = run_staging(DIGEST, tool, inputs, ["x"])
    assert not report.passed
    assert [f.check for f in report.findings] == ["staging-exit-code"]


def test_timeout_fails(dirs, fake_sandbox) -> None:
    tool, inputs = dirs
    _, state = fake_sandbox
    state["result"] = ok_result(exit_code=None, timed_out=True)
    report = run_staging(DIGEST, tool, inputs, ["x"])
    assert "staging-timeout" in [f.check for f in report.findings]


def test_truncated_output_fails(dirs, fake_sandbox) -> None:
    tool, inputs = dirs
    _, state = fake_sandbox
    state["result"] = ok_result(stdout_truncated=True)
    report = run_staging(DIGEST, tool, inputs, ["x"])
    assert [f.check for f in report.findings] == ["staging-output-truncated"]


def test_changed_inputs_fail(dirs, fake_sandbox) -> None:
    tool, inputs = dirs
    _, state = fake_sandbox
    state["side_effect"] = lambda: (inputs / "log.txt").write_text("tampered\n")
    report = run_staging(DIGEST, tool, inputs, ["x"])
    assert [f.check for f in report.findings] == ["staging-inputs-modified"]


def test_changed_tool_files_fail(dirs, fake_sandbox) -> None:
    tool, inputs = dirs
    _, state = fake_sandbox
    state["side_effect"] = lambda: (tool / "tool.py").write_text("print(1)\n")
    report = run_staging(DIGEST, tool, inputs, ["x"])
    assert [f.check for f in report.findings] == ["staging-tool-modified"]


def test_all_problems_reported_together(dirs, fake_sandbox) -> None:
    tool, inputs = dirs
    _, state = fake_sandbox
    state["result"] = ok_result(exit_code=1, stderr_truncated=True)
    state["side_effect"] = lambda: (inputs / "log.txt").write_text("tampered\n")
    report = run_staging(DIGEST, tool, inputs, ["x"])
    assert {f.check for f in report.findings} == {
        "staging-exit-code",
        "staging-output-truncated",
        "staging-inputs-modified",
    }
