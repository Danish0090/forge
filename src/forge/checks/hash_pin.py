"""Hash pin: fingerprint every file in a tool folder, so any later change is detected.

SECURITY-CRITICAL. Changes here need the project owner's review.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from forge.checks.finding import Finding

_SKIP_DIRS = frozenset({"__pycache__"})


def tree_hash(tool_dir: Path) -> str:
    """SHA-256 over every file's relative path and bytes, in sorted order."""
    digest = hashlib.sha256()
    for path in sorted(_files(tool_dir)):
        digest.update(path.relative_to(tool_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def verify_pin(tool_dir: Path, pinned: str) -> list[Finding]:
    """Empty list when the folder still matches the pinned hash, one finding when it has drifted."""
    if tree_hash(tool_dir) == pinned:
        return []
    return [Finding("hash-pin", "tool files changed since they were pinned")]


def _files(tool_dir: Path) -> list[Path]:
    return [
        p
        for p in tool_dir.rglob("*")
        if p.is_file() and not any(part in _SKIP_DIRS for part in p.relative_to(tool_dir).parts)
    ]
