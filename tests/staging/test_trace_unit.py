"""Staging traces: what a staging run did, kept as evidence. The sandbox is faked, so no Docker is needed.

A trace records the command, exit code, wall time and capped output of one sandbox run. It is
evidence for a reviewer and for the benchmark report, not a decision: nothing here approves a tool.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from forge.sandbox.policy import SandboxPolicy
from forge.sandbox.runner import SandboxResult
from forge.staging import cases as cases_mod
from forge.staging import stager
from forge.staging.cases import StagingCase, run_cases
from forge.staging.stager import run_staging
from forge.staging.trace import MAX_ARGV_CHARS, MAX_OUTPUT_CHARS, StagingTrace, make_trace, summarize_traces

DIGEST = "python@sha256:" + "a" * 64


def result(stdout: str = "3\n", **kw: object) -> SandboxResult:
    return replace(SandboxResult("forge-x", 0, stdout, "", False, False, False), **kw)  # type: ignore[arg-type]


def test_make_trace_keeps_the_facts_and_caps_the_text() -> None:
    t = make_trace("main", ["python", "/app/tool.py", "x" * 1000], result("o" * 5000, stderr="e" * 5000, exit_code=2), 0.25)
    assert (t.label, t.exit_code, t.seconds, t.timed_out) == ("main", 2, 0.25, False)
    assert len(t.stdout) <= MAX_OUTPUT_CHARS and len(t.stderr) <= MAX_OUTPUT_CHARS
    assert len(" ".join(t.argv)) <= MAX_ARGV_CHARS + 40
    assert t.stdout_bytes == 5000  # the real size is kept even though the text is cut


def test_a_killed_run_is_recorded_as_timed_out() -> None:
    t = make_trace("main", ["x"], result("", exit_code=None, timed_out=True), 10.0)
    assert t.timed_out and t.exit_code is None


def test_summary_is_one_readable_line_per_group() -> None:
    traces = (
        make_trace("main", ["x"], result("12345"), 0.5),
        make_trace("case 1", ["x"], result("3"), 0.25),
        make_trace("case 2", ["x"], result("4", exit_code=1), 0.25),
    )
    text = summarize_traces(traces)
    assert "main run" in text and "exit 0" in text and "0.5s" in text
    assert "2 test case runs" in text and "1 failed" in text


def test_summary_of_nothing_is_empty() -> None:
    assert summarize_traces(()) == ""


# --- the stager and the case runner attach traces ---------------------------------------------------


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    tool = tmp_path / "tool"
    tool.mkdir()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    return tool, inputs


def test_a_staging_run_records_one_main_trace(dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    tool, inputs = dirs
    monkeypatch.setattr(stager, "run_in_sandbox", lambda policy, command: result("done\n"))
    report = run_staging(DIGEST, tool, inputs, ["python", "/app/tool.py", "{}"])
    (trace,) = report.traces
    assert trace.label == "main" and trace.stdout == "done\n" and trace.exit_code == 0
    assert trace.seconds >= 0


def test_a_failed_main_run_still_leaves_its_trace(dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    tool, inputs = dirs
    monkeypatch.setattr(stager, "run_in_sandbox", lambda policy, command: result("", stderr="Traceback", exit_code=1))
    report = run_staging(DIGEST, tool, inputs, ["x"])
    assert not report.passed
    assert report.traces[0].stderr == "Traceback" and report.traces[0].exit_code == 1


def test_case_runs_add_their_own_traces(dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    tool, inputs = dirs
    monkeypatch.setattr(stager, "run_in_sandbox", lambda policy, command: result("3\n"))
    monkeypatch.setattr(cases_mod, "run_in_sandbox", lambda policy, command: result("3\n"))
    case = StagingCase(args={"p": 1}, expect="3", files={"a.txt": "x"})
    report = run_staging(DIGEST, tool, inputs, ["x"], cases=[case, case], case_command=lambda a: ["python", "/app/tool.py", "A"])
    assert [t.label for t in report.traces] == ["main", "case 1", "case 2"]


def test_run_cases_traces_even_failing_cases(dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    tool, _ = dirs
    monkeypatch.setattr(cases_mod, "run_in_sandbox", lambda policy, command: result("7\n"))
    report = run_cases(DIGEST, tool, [StagingCase(args={}, expect="3")], lambda a: ["x"])
    assert [t.label for t in report.traces] == ["case 1"] and report.traces[0].stdout == "7\n"


def test_a_trace_is_plain_data_that_serialises() -> None:
    import dataclasses
    import json

    t = make_trace("main", ["python", "/app/tool.py", "{}"], result(), 0.1)
    assert isinstance(t, StagingTrace)
    json.dumps(dataclasses.asdict(t))
