"""Integration tests: forger-written cases in real containers. Need Docker and FORGE_SANDBOX_IMAGE."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.staging.cases import StagingCase, run_cases

pytestmark = pytest.mark.docker

WORD_COUNT = (
    "import argparse, json\nfrom pathlib import Path\n"
    "p = argparse.ArgumentParser(); p.add_argument('payload')\n"
    "print(len(Path(json.loads(p.parse_args().payload)['path']).read_text().split()))\n"
)
WRITER = (
    "from pathlib import Path\n"
    "Path('/inputs/new.txt').write_text('x')\n"
    "print('3')\n"
)


def command_for(args: dict) -> list[str]:
    return ["python", "/app/tool.py", json.dumps(args)]


def make_tool(tmp_path: Path, code: str) -> Path:
    d = tmp_path / "tool"
    d.mkdir()
    (d / "tool.py").write_text(code)
    return d


def case(expect: str, text: str = "one two three\n") -> StagingCase:
    return StagingCase(args={"path": "/inputs/a.txt"}, expect=expect, files={"a.txt": text})


def test_a_correct_tool_passes_its_cases(tmp_path: Path, sandbox_image: str) -> None:
    tool = make_tool(tmp_path, WORD_COUNT)
    report = run_cases(sandbox_image, tool, [case("3"), case("1", "solo\n")], command_for, timeout_s=20)
    assert report.findings == ()
    assert (report.run, report.passed) == (2, 2)


def test_a_wrong_expectation_is_caught(tmp_path: Path, sandbox_image: str) -> None:
    tool = make_tool(tmp_path, WORD_COUNT)
    report = run_cases(sandbox_image, tool, [case("4")], command_for, timeout_s=20)
    assert [f.check for f in report.findings] == ["staging-test-failed"]


def test_fixtures_are_read_only_inside_the_container(tmp_path: Path, sandbox_image: str) -> None:
    tool = make_tool(tmp_path, WRITER)
    report = run_cases(sandbox_image, tool, [case("3")], command_for, timeout_s=20)
    assert [f.check for f in report.findings] == ["staging-test-error"]
