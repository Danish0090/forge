"""Hash pin: a fingerprint of the tool's files, so any later change is caught."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.checks.hash_pin import tree_hash, verify_pin


@pytest.fixture
def tool_dir(tmp_path: Path) -> Path:
    d = tmp_path / "tool"
    d.mkdir()
    (d / "tool.py").write_text("print('hi')\n")
    return d


def test_hash_is_a_sha256_hex_string(tool_dir: Path) -> None:
    digest = tree_hash(tool_dir)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_is_stable_for_unchanged_files(tool_dir: Path) -> None:
    assert tree_hash(tool_dir) == tree_hash(tool_dir)


def test_hash_changes_when_content_changes(tool_dir: Path) -> None:
    before = tree_hash(tool_dir)
    (tool_dir / "tool.py").write_text("print('bye')\n")
    assert tree_hash(tool_dir) != before


def test_hash_changes_when_a_file_is_renamed(tool_dir: Path) -> None:
    before = tree_hash(tool_dir)
    (tool_dir / "tool.py").rename(tool_dir / "other.py")
    assert tree_hash(tool_dir) != before


def test_hash_ignores_pycache(tool_dir: Path) -> None:
    before = tree_hash(tool_dir)
    cache = tool_dir / "__pycache__"
    cache.mkdir()
    (cache / "tool.cpython-312.pyc").write_bytes(b"junk")
    assert tree_hash(tool_dir) == before


def test_verify_pin_passes_on_match(tool_dir: Path) -> None:
    assert verify_pin(tool_dir, tree_hash(tool_dir)) == []


def test_verify_pin_flags_drift(tool_dir: Path) -> None:
    pinned = tree_hash(tool_dir)
    (tool_dir / "tool.py").write_text("import os\n")
    findings = verify_pin(tool_dir, pinned)
    assert [f.check for f in findings] == ["hash-pin"]
