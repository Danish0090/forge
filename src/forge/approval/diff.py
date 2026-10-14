"""Render what a human reviews: a unified diff of the tool folder, safe to print to a terminal.

SECURITY-CRITICAL. Changes here need the project owner's review.

The reviewer must see everything that the hash pin covers, and nothing in the output may hide
text from them. So this module walks the same files the pin hashes, and escapes every control
or invisible character (ANSI escapes, carriage returns, bidi overrides, zero-width characters).
"""

from __future__ import annotations

import difflib
import hashlib
import unicodedata
from pathlib import Path

# Same file set as the pin, so nothing that is hashed can be left out of the review.
from forge.checks.hash_pin import _files


def render_diff(old_dir: Path | None, new_dir: Path) -> str:
    """Unified diff from `old_dir` (None for a brand-new tool) to `new_dir`."""
    old = _read_tree(old_dir) if old_dir is not None else {}
    new = _read_tree(new_dir)
    chunks: list[str] = []
    for name in sorted(old.keys() | new.keys()):
        before, after = old.get(name), new.get(name)
        if before == after:
            continue
        chunks.append(_diff_file(name, before, after))
    return "\n".join(chunks) if chunks else "(no changes)"


def escape_text(text: str) -> str:
    """Make control, format, and line-separator characters visible. Tabs and newlines stay."""
    out: list[str] = []
    for ch in text:
        if ch in "\t\n":
            out.append(ch)
        elif unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in ("Zl", "Zp"):
            code = ord(ch)
            out.append(f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
        else:
            out.append(ch)
    return "".join(out)


def _read_tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in _files(root)}


def _diff_file(name: str, before: bytes | None, after: bytes | None) -> str:
    old_text, new_text = _decode(before), _decode(after)
    if old_text is None or new_text is None:
        return "\n".join(
            f"--- {escape_text(name)}\n+++ {escape_text(name)}\n"
            f"binary file {label}: {_describe(data)}"
            for label, data in (("before", before), ("after", after))
            if data is not None
        )
    old_lines = [] if before is None else escape_text(old_text).split("\n")
    new_lines = [] if after is None else escape_text(new_text).split("\n")
    safe = escape_text(name)
    return "\n".join(difflib.unified_diff(old_lines, new_lines, safe, safe, lineterm=""))


def _decode(data: bytes | None) -> str | None:
    if data is None:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _describe(data: bytes) -> str:
    return f"{len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}"
