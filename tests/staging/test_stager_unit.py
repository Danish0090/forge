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


# --- forger-written test cases run after a clean staging run ---------------------------------------

from forge.checks.finding import Finding  # noqa: E402
from forge.staging.cases import CaseReport, StagingCase  # noqa: E402

CASE = StagingCase(args={"path": "/inputs/a.txt"}, expect="3", files={"a.txt": "a b c\n"})


def encode(args: dict) -> list[str]:
    return ["python", "/app/tool.py", "ENCODED"]


def test_cases_run_after_a_clean_staging_run(dirs, fake_sandbox, monkeypatch) -> None:
    tool, inputs = dirs
    seen: list[tuple] = []

    def fake_cases(image, tool_dir, cases, command_for, *, timeout_s):
        seen.append((image, tool_dir, tuple(cases), command_for, timeout_s))
        return CaseReport((), 1, 1)

    monkeypatch.setattr(stager, "run_cases", fake_cases)
    report = run_staging(DIGEST, tool, inputs, ["x"], timeout_s=7, cases=[CASE], case_command=encode)
    assert report.passed
    assert (report.cases_run, report.cases_passed) == (1, 1)
    assert seen == [(DIGEST, tool, (CASE,), encode, 7)]


def test_failing_cases_make_staging_fail(dirs, fake_sandbox, monkeypatch) -> None:
    tool, inputs = dirs
    finding = Finding("staging-test-failed", "case 1: expected '3', got '4'")
    monkeypatch.setattr(stager, "run_cases", lambda *a, **k: CaseReport((finding,), 1, 0))
    report = run_staging(DIGEST, tool, inputs, ["x"], cases=[CASE], case_command=encode)
    assert not report.passed
    assert report.findings == (finding,)
    assert (report.cases_run, report.cases_passed) == (1, 0)


def test_cases_are_skipped_when_the_main_run_already_failed(dirs, fake_sandbox, monkeypatch) -> None:
    tool, inputs = dirs
    _, state = fake_sandbox
    state["result"] = ok_result(exit_code=1)
    called: list[int] = []
    monkeypatch.setattr(stager, "run_cases", lambda *a, **k: called.append(1) or CaseReport((), 1, 1))
    report = run_staging(DIGEST, tool, inputs, ["x"], cases=[CASE], case_command=encode)
    assert [f.check for f in report.findings] == ["staging-exit-code"]
    assert called == []
    assert (report.cases_run, report.cases_passed) == (0, 0)


def test_no_cases_leaves_behaviour_unchanged(dirs, fake_sandbox) -> None:
    tool, inputs = dirs
    report = run_staging(DIGEST, tool, inputs, ["x"])
    assert report.passed and (report.cases_run, report.cases_passed) == (0, 0)


def test_cases_need_a_way_to_build_the_command(dirs, fake_sandbox) -> None:
    tool, inputs = dirs
    with pytest.raises(ValueError):
        run_staging(DIGEST, tool, inputs, ["x"], cases=[CASE])
