"""Diff rendering for the approval gate. No Docker needed."""

from __future__ import annotations

from pathlib import Path

from forge.approval.diff import render_diff


def make(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    root = tmp_path / name
    root.mkdir()
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def test_new_tool_shows_every_line_as_added(tmp_path: Path) -> None:
    new = make(tmp_path, "new", {"tool.py": b"print('hi')\n"})
    out = render_diff(None, new)
    assert "+++ tool.py" in out
    assert "+print('hi')" in out


def test_update_shows_only_changes(tmp_path: Path) -> None:
    old = make(tmp_path, "old", {"tool.py": b"a = 1\nb = 2\n"})
    new = make(tmp_path, "new", {"tool.py": b"a = 1\nb = 3\n"})
    out = render_diff(old, new)
    assert "-b = 2" in out and "+b = 3" in out


def test_removed_and_added_files_are_shown(tmp_path: Path) -> None:
    old = make(tmp_path, "old", {"gone.py": b"x = 1\n"})
    new = make(tmp_path, "new", {"fresh.py": b"y = 2\n"})
    out = render_diff(old, new)
    assert "-x = 1" in out and "+y = 2" in out


def test_terminal_escapes_and_hidden_characters_are_made_visible(tmp_path: Path) -> None:
    payload = "ok = 1\x1b[2K\rhidden‮​\n".encode()
    new = make(tmp_path, "new", {"tool.py": payload})
    out = render_diff(None, new)
    assert "\x1b" not in out and "\r" not in out
    assert "‮" not in out and "​" not in out
    assert "\\x1b" in out and "\\u202e" in out


def test_binary_file_is_flagged_with_its_hash_not_dumped(tmp_path: Path) -> None:
    new = make(tmp_path, "new", {"blob.bin": b"\xff\xfe\x00\x01"})
    out = render_diff(None, new)
    assert "binary file" in out and "blob.bin" in out
    assert "sha256" in out


def test_identical_folders_say_so(tmp_path: Path) -> None:
    old = make(tmp_path, "old", {"tool.py": b"x = 1\n"})
    new = make(tmp_path, "new", {"tool.py": b"x = 1\n"})
    assert "no changes" in render_diff(old, new)


def test_pycache_is_ignored_like_the_hash_pin(tmp_path: Path) -> None:
    new = make(tmp_path, "new", {"tool.py": b"x = 1\n", "__pycache__/tool.pyc": b"\x00junk"})
    assert "pyc" not in render_diff(None, new)
