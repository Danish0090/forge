"""Static scan of generated tool code. Reads source only; nothing is executed."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.checks.static_scan import scan_source, scan_tool_dir


def test_clean_code_passes() -> None:
    src = "import json\n\n\ndef convert(km: float) -> float:\n    return km * 0.621371\n"
    assert scan_source(src) == []


@pytest.mark.parametrize(
    "module",
    ["socket", "subprocess", "ctypes", "urllib", "http", "requests", "importlib", "os", "sys"],
)
def test_denied_import_is_flagged(module: str) -> None:
    findings = scan_source(f"import {module}\n")
    assert [f.check for f in findings] == ["denied-import"]


def test_denied_from_import_is_flagged() -> None:
    findings = scan_source("from urllib.request import urlopen\n")
    assert [f.check for f in findings] == ["denied-import"]


def test_submodule_of_denied_package_is_flagged() -> None:
    findings = scan_source("import os.path\n")
    assert [f.check for f in findings] == ["denied-import"]


@pytest.mark.parametrize("name", ["eval", "exec", "compile", "__import__", "breakpoint"])
def test_denied_builtin_call_is_flagged(name: str) -> None:
    findings = scan_source(f"x = {name}('1')\n")
    assert [f.check for f in findings] == ["denied-call"]


@pytest.mark.parametrize(
    "attr",
    ["__subclasses__", "__globals__", "__builtins__", "__code__", "__class__", "__bases__", "__mro__"],
)
def test_sandbox_escape_attribute_is_flagged(attr: str) -> None:
    findings = scan_source(f"x = object.{attr}\n")
    assert [f.check for f in findings] == ["denied-attribute"]


def test_syntax_error_is_flagged() -> None:
    findings = scan_source("def broken(:\n")
    assert [f.check for f in findings] == ["syntax"]


def test_findings_carry_line_numbers() -> None:
    findings = scan_source("import json\nimport socket\n")
    assert findings[0].line == 2


def test_scan_tool_dir_reads_every_python_file(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("import json\n")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "bad.py").write_text("import socket\n")

    findings = scan_tool_dir(tmp_path)

    assert len(findings) == 1
    assert findings[0].path == "lib/bad.py"
