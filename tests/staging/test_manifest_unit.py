"""Unit tests for the capability manifest. No Docker.

A manifest is a data file (manifest.json) in the tool folder declaring which input files the tool
reads. The host enforces it by mounting a view that holds copies of only those files. The network
stays off whatever the manifest says, so a manifest that lists a domain is rejected, not honoured.
The manifest is inside the hashed folder, so it is approved, pinned and tamper-checked with the code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from forge.staging.manifest import (
    MANIFEST_NAME,
    MAX_MANIFEST_BYTES,
    MAX_READS,
    MAX_VIEW_BYTES,
    Manifest,
    ManifestError,
    describe,
    input_view,
    load_manifest,
    parse_manifest,
)


def doc(**kw: object) -> str:
    base: dict[str, object] = {"reads": ["notes.txt"]}
    base.update(kw)
    return json.dumps(base)


# --- parse_manifest: the forger is untrusted ------------------------------------------------------


def test_parse_accepts_a_valid_manifest() -> None:
    assert parse_manifest(doc()) == Manifest(reads=("notes.txt",), network=())
    assert parse_manifest(doc(network=[])).network == ()


def test_an_empty_reads_list_is_a_valid_declaration() -> None:
    assert parse_manifest(doc(reads=[])).reads == ()  # a tool that reads nothing


def test_subfolders_are_allowed_a_few_levels_deep() -> None:
    assert parse_manifest(doc(reads=["a/b.txt", "a/b/c.txt"])).reads == ("a/b.txt", "a/b/c.txt")


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        "null",
        '"reads"',
        "{}",  # reads must be declared, even if empty
        doc(reads="notes.txt"),
        doc(reads=[1]),
        doc(reads=[None]),
        doc(reads=["a"] * 2),  # duplicate
        doc(reads=[f"f{i}.txt" for i in range(MAX_READS + 1)]),
        doc(extra=1),  # unknown keys are refused, not ignored
        doc(network="none"),
        "x" * (MAX_MANIFEST_BYTES + 1),
    ],
)
def test_parse_rejects_bad_shapes(text: str) -> None:
    with pytest.raises(ManifestError):
        parse_manifest(text)


@pytest.mark.parametrize(
    "name",
    ["../secret.txt", "a/../b.txt", "/etc/passwd", "a//b.txt", "", ".hidden", "a\\b.txt", "a b.txt", "x" * 65, "a\x00b", "a/b/c/d.txt", ".", "..", "~/x"],
)
def test_parse_rejects_unsafe_paths(name: str) -> None:
    with pytest.raises(ManifestError):
        parse_manifest(doc(reads=[name]))


@pytest.mark.parametrize("domains", [["evil.example"], ["203.0.113.9"], ["*"], ["api.example.com:443"]])
def test_any_domain_is_rejected_because_the_sandbox_has_no_network(domains: list[str]) -> None:
    with pytest.raises(ManifestError) as exc:
        parse_manifest(doc(network=domains))
    assert "no network" in str(exc.value)


def test_describe_is_one_readable_line() -> None:
    assert describe(Manifest(("a.txt", "b/c.txt"))) == "reads a.txt, b/c.txt; network none"
    assert describe(Manifest(())) == "reads nothing; network none"


# --- load_manifest ----------------------------------------------------------------------------------


def test_load_returns_none_when_there_is_no_manifest(tmp_path: Path) -> None:
    assert load_manifest(tmp_path) is None


def test_load_reads_the_file_in_the_tool_folder(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_NAME).write_text(doc())
    assert load_manifest(tmp_path) == Manifest(("notes.txt",))


def test_load_refuses_a_symlinked_manifest(tmp_path: Path) -> None:
    (tmp_path / "tool.py").write_text("print(1)\n")
    (tmp_path / MANIFEST_NAME).symlink_to("tool.py")
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_load_refuses_an_oversize_manifest_before_reading_it(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_NAME).write_text("x" * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_load_refuses_a_manifest_that_is_not_utf8(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_NAME).write_bytes(b"\xff\xfe{}")
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_load_surfaces_parse_errors(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_NAME).write_text(doc(network=["evil.example"]))
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


# --- input_view: the enforcement --------------------------------------------------------------------


@pytest.fixture
def inputs(tmp_path: Path) -> Path:
    d = tmp_path / "inputs"
    (d / "sub").mkdir(parents=True)
    (d / "notes.txt").write_text("one two\n")
    (d / "secret.txt").write_text("TOP SECRET\n")
    (d / "sub" / "deep.txt").write_text("deep\n")
    return d


def test_without_a_manifest_the_whole_folder_is_visible(inputs: Path) -> None:
    with input_view(None, inputs) as view:
        assert view.path == inputs and view.missing == ()


def test_only_declared_files_are_visible(inputs: Path) -> None:
    with input_view(Manifest(("notes.txt",)), inputs) as view:
        assert sorted(p.name for p in view.path.rglob("*") if p.is_file()) == ["notes.txt"]
        assert (view.path / "notes.txt").read_text() == "one two\n"
        assert not (view.path / "secret.txt").exists()
        assert view.path != inputs
        assert view.missing == ()


def test_subfolder_files_keep_their_relative_path(inputs: Path) -> None:
    with input_view(Manifest(("sub/deep.txt",)), inputs) as view:
        assert (view.path / "sub" / "deep.txt").read_text() == "deep\n"


def test_the_view_is_a_copy_so_the_real_inputs_cannot_change(inputs: Path) -> None:
    with input_view(Manifest(("notes.txt",)), inputs) as view:
        (view.path / "notes.txt").write_text("changed")
    assert (inputs / "notes.txt").read_text() == "one two\n"


def test_the_view_is_removed_afterwards(inputs: Path) -> None:
    with input_view(Manifest(("notes.txt",)), inputs) as view:
        path = view.path
    assert not path.exists()


def test_the_view_is_removed_even_when_the_body_raises(inputs: Path) -> None:
    with pytest.raises(RuntimeError):
        with input_view(Manifest(("notes.txt",)), inputs) as view:
            path = view.path
            raise RuntimeError("boom")
    assert not path.exists()


def test_declared_files_that_do_not_exist_are_reported_not_invented(inputs: Path) -> None:
    with input_view(Manifest(("notes.txt", "ghost.txt")), inputs) as view:
        assert view.missing == ("ghost.txt",)
        assert not (view.path / "ghost.txt").exists()


def test_a_symlinked_input_is_refused_not_followed(inputs: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret\n")
    (inputs / "link.txt").symlink_to(outside)
    with pytest.raises(ManifestError):
        with input_view(Manifest(("link.txt",)), inputs):
            pass


def test_a_symlinked_parent_folder_is_refused(inputs: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("host secret\n")
    (inputs / "dir").symlink_to(outside)
    with pytest.raises(ManifestError):
        with input_view(Manifest(("dir/x.txt",)), inputs):
            pass


def test_a_declared_folder_is_not_a_file(inputs: Path) -> None:
    with pytest.raises(ManifestError):
        with input_view(Manifest(("sub",)), inputs):
            pass


def test_the_total_size_is_capped(inputs: Path) -> None:
    (inputs / "big.bin").write_bytes(b"x" * (MAX_VIEW_BYTES + 1))
    with pytest.raises(ManifestError):
        with input_view(Manifest(("big.bin",)), inputs):
            pass


def test_view_files_are_world_readable_so_the_unprivileged_sandbox_user_can_read_them(inputs: Path) -> None:
    with input_view(Manifest(("notes.txt",)), inputs) as view:
        assert (os.stat(view.path).st_mode & 0o005) == 0o005
        assert (os.stat(view.path / "notes.txt").st_mode & 0o004) == 0o004
