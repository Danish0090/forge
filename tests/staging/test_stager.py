"""Integration tests: staging in a real container. Need Docker and FORGE_SANDBOX_IMAGE."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.staging.stager import run_staging

pytestmark = pytest.mark.docker

COUNT_LINES = (
    "from pathlib import Path\n"
    "print(sum(1 for _ in Path('/inputs/log.txt').open()))\n"
)
WRITE_INPUT = (
    "from pathlib import Path\n"
    "try:\n"
    "    Path('/inputs/new.txt').write_text('x')\n"
    "    print('WROTE')\n"
    "except OSError:\n"
    "    print('DENIED')\n"
)


@pytest.fixture
def inputs(tmp_path: Path) -> Path:
    d = tmp_path / "inputs"
    d.mkdir()
    (d / "log.txt").write_text("a\nb\nc\n")
    return d


def make_tool(tmp_path: Path, code: str) -> Path:
    d = tmp_path / "tool"
    d.mkdir()
    (d / "tool.py").write_text(code)
    return d


def test_tool_reads_real_inputs(tmp_path: Path, inputs: Path, sandbox_image: str) -> None:
    tool = make_tool(tmp_path, COUNT_LINES)
    report = run_staging(sandbox_image, tool, inputs, ["python", "/app/tool.py"], timeout_s=20)
    assert report.passed, report.findings
    assert report.result.stdout.strip() == "3"


def test_tool_cannot_write_to_inputs(tmp_path: Path, inputs: Path, sandbox_image: str) -> None:
    tool = make_tool(tmp_path, WRITE_INPUT)
    report = run_staging(sandbox_image, tool, inputs, ["python", "/app/tool.py"], timeout_s=20)
    assert report.result.stdout.strip() == "DENIED"
    assert not (inputs / "new.txt").exists()
