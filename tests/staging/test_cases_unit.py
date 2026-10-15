"""Unit tests for forger-written test cases. The sandbox is faked, so no Docker daemon is needed.

A case is data (fixture files, tool arguments, expected output), never code. The host writes the
fixtures, mounts them read-only in a throwaway container and compares the tool's stdout.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from forge.sandbox.policy import SandboxPolicy
from forge.sandbox.runner import SandboxResult
from forge.staging import cases as cases_mod
from forge.staging.cases import (
    MAX_CASES,
    MAX_EXPECT_CHARS,
    MAX_FILE_CHARS,
    MAX_FILES,
    CaseError,
    StagingCase,
    parse_cases,
    run_cases,
)

DIGEST = "python@sha256:" + "a" * 64


def command_for(args: dict) -> list[str] | None:
    return ["python", "/app/tool.py", "ARGS"]


def ok_result(stdout: str = "3\n", **overrides: object) -> SandboxResult:
    base = SandboxResult("forge-x", 0, stdout, "", False, False, False)
    return replace(base, **overrides)  # type: ignore[arg-type]


def good_case(**overrides: object) -> dict:
    case = {"files": {"a.txt": "one two three\n"}, "args": {"path": "/inputs/a.txt"}, "expect": "3"}
    case.update(overrides)
    return case


# --- parse_cases: the forger is untrusted, so the shape is validated strictly ---------------------


def test_parse_accepts_a_valid_case() -> None:
    (case,) = parse_cases([good_case()])
    assert case == StagingCase(args={"path": "/inputs/a.txt"}, expect="3", files={"a.txt": "one two three\n"})


@pytest.mark.parametrize("raw", [None, []])
def test_parse_treats_missing_tests_as_none(raw: object) -> None:
    assert parse_cases(raw) == ()


@pytest.mark.parametrize(
    "raw",
    [
        "not a list",
        {"files": {}},
        ["not a dict"],
        [good_case(expect=None)],
        [good_case(expect=3)],
        [good_case(expect="")],
        [good_case(expect="x" * (MAX_EXPECT_CHARS + 1))],
        [good_case(args="not a dict")],
        [good_case(files="not a dict")],
        [good_case(files={"a.txt": 5})],
        [good_case(files={"a.txt": "x" * (MAX_FILE_CHARS + 1)})],
        [good_case(files={f"f{i}.txt": "x" for i in range(MAX_FILES + 1)})],
        [good_case()] * (MAX_CASES + 1),
    ],
)
def test_parse_rejects_bad_shapes(raw: object) -> None:
    with pytest.raises(CaseError):
        parse_cases(raw)


@pytest.mark.parametrize("name", ["../x.txt", "a/b.txt", "/etc/passwd", "", ".hidden", "a b.txt", "x" * 65, "a\x00b"])
def test_parse_rejects_unsafe_fixture_names(name: str) -> None:
    with pytest.raises(CaseError):
        parse_cases([good_case(files={name: "x"})])


def test_a_case_with_no_files_is_allowed() -> None:
    (case,) = parse_cases([good_case(files={})])
    assert case.files == {}


# --- run_cases ------------------------------------------------------------------------------------


@pytest.fixture
def tool(tmp_path: Path) -> Path:
    d = tmp_path / "tool"
    d.mkdir()
    (d / "tool.py").write_text("print(3)\n")
    return d


@pytest.fixture
def fake_sandbox(monkeypatch: pytest.MonkeyPatch):
    seen: list[dict] = []
    state = {"results": [ok_result()], "side_effect": None}

    def fake(policy: SandboxPolicy, command: list[str]) -> SandboxResult:
        inputs = policy.inputs_dir
        assert inputs is not None
        seen.append(
            {
                "inputs": inputs,
                "files": {p.name: p.read_text() for p in inputs.iterdir()},
                "command": list(command),
                "tool_dir": policy.tool_dir,
            }
        )
        if state["side_effect"]:
            state["side_effect"]()
        results = state["results"]
        return results[min(len(seen) - 1, len(results) - 1)]  # type: ignore[index]

    monkeypatch.setattr(cases_mod, "run_in_sandbox", fake)
    return seen, state


def test_fixtures_are_written_and_mounted_for_each_case(tool, fake_sandbox) -> None:
    seen, _ = fake_sandbox
    cases = parse_cases([good_case(), good_case(files={"b.txt": "x y\n"})])
    run_cases(DIGEST, tool, cases, command_for)
    assert seen[0]["files"] == {"a.txt": "one two three\n"}
    assert seen[1]["files"] == {"b.txt": "x y\n"}
    assert all(s["tool_dir"] == tool for s in seen)
    assert seen[0]["command"] == ["python", "/app/tool.py", "ARGS"]


def test_fixture_folders_are_removed_afterwards(tool, fake_sandbox) -> None:
    seen, _ = fake_sandbox
    run_cases(DIGEST, tool, parse_cases([good_case()]), command_for)
    assert not seen[0]["inputs"].exists()


def test_matching_output_passes_and_is_counted(tool, fake_sandbox) -> None:
    report = run_cases(DIGEST, tool, parse_cases([good_case(), good_case()]), command_for)
    assert report.findings == ()
    assert (report.run, report.passed) == (2, 2)


def test_output_is_compared_after_normalising_whitespace(tool, fake_sandbox) -> None:
    _, state = fake_sandbox
    state["results"] = [ok_result("  a   b \n\n")]
    report = run_cases(DIGEST, tool, parse_cases([good_case(expect="a b")]), command_for)
    assert report.findings == ()


def test_wrong_output_is_a_finding_with_both_values(tool, fake_sandbox) -> None:
    _, state = fake_sandbox
    state["results"] = [ok_result("7\n")]
    report = run_cases(DIGEST, tool, parse_cases([good_case(expect="3")]), command_for)
    assert [f.check for f in report.findings] == ["staging-test-failed"]
    assert "case 1" in report.findings[0].message
    assert "'3'" in report.findings[0].message and "'7'" in report.findings[0].message
    assert (report.run, report.passed) == (1, 0)


def test_a_failing_case_does_not_hide_the_rest(tool, fake_sandbox) -> None:
    _, state = fake_sandbox
    state["results"] = [ok_result("7\n"), ok_result("3\n")]
    report = run_cases(DIGEST, tool, parse_cases([good_case(), good_case()]), command_for)
    assert (report.run, report.passed) == (2, 1)


def test_long_output_is_cut_in_the_finding(tool, fake_sandbox) -> None:
    _, state = fake_sandbox
    state["results"] = [ok_result("z" * 5000)]
    report = run_cases(DIGEST, tool, parse_cases([good_case()]), command_for)
    assert len(report.findings[0].message) < 400


def test_crash_and_timeout_are_errors_not_mismatches(tool, fake_sandbox) -> None:
    _, state = fake_sandbox
    state["results"] = [ok_result("", exit_code=1), ok_result("", exit_code=None, timed_out=True)]
    report = run_cases(DIGEST, tool, parse_cases([good_case(), good_case()]), command_for)
    assert [f.check for f in report.findings] == ["staging-test-error", "staging-test-error"]


def test_arguments_that_cannot_be_encoded_fail_the_case(tool, fake_sandbox) -> None:
    seen, _ = fake_sandbox
    report = run_cases(DIGEST, tool, parse_cases([good_case()]), lambda args: None)
    assert [f.check for f in report.findings] == ["staging-test-invalid"]
    assert seen == []  # no container was started


def test_a_tool_that_rewrites_itself_is_reported(tool, fake_sandbox) -> None:
    _, state = fake_sandbox
    state["side_effect"] = lambda: (tool / "tool.py").write_text("print(99)\n")
    report = run_cases(DIGEST, tool, parse_cases([good_case()]), command_for)
    assert "staging-tool-modified" in [f.check for f in report.findings]


def test_no_cases_means_nothing_runs(tool, fake_sandbox) -> None:
    seen, _ = fake_sandbox
    report = run_cases(DIGEST, tool, (), command_for)
    assert report.findings == () and (report.run, report.passed) == (0, 0)
    assert seen == []


# --- the capability manifest also limits what a test case can see ---------------------------------------

from forge.staging.manifest import Manifest  # noqa: E402


def test_fixtures_are_filtered_through_the_manifest(tool, fake_sandbox) -> None:
    seen, _ = fake_sandbox
    case = good_case(files={"a.txt": "one two three\n", "extra.txt": "should be hidden\n"})
    run_cases(DIGEST, tool, parse_cases([case]), command_for, manifest=Manifest(("a.txt",)))
    assert seen[0]["files"] == {"a.txt": "one two three\n"}


def test_a_manifest_that_declares_nothing_mounts_an_empty_folder(tool, fake_sandbox) -> None:
    seen, _ = fake_sandbox
    run_cases(DIGEST, tool, parse_cases([good_case()]), command_for, manifest=Manifest(()))
    assert seen[0]["files"] == {}


def test_without_a_manifest_every_fixture_is_mounted(tool, fake_sandbox) -> None:
    seen, _ = fake_sandbox
    run_cases(DIGEST, tool, parse_cases([good_case(files={"a.txt": "x", "b.txt": "y"})]), command_for)
    assert set(seen[0]["files"]) == {"a.txt", "b.txt"}


def test_view_folders_for_cases_are_removed(tool, fake_sandbox, tmp_path: Path) -> None:
    import tempfile
    before = set(Path(tempfile.gettempdir()).glob("forge-view-*"))
    run_cases(DIGEST, tool, parse_cases([good_case()]), command_for, manifest=Manifest(("a.txt",)))
    assert set(Path(tempfile.gettempdir()).glob("forge-view-*")) == before
