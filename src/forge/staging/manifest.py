"""Capability manifest: the input files a tool declares it reads, enforced by what the sandbox can see.

SECURITY-CRITICAL. Changes here need the project owner's review.

A manifest is `manifest.json` inside the tool folder, so it is hashed, pinned, shown in the diff the
human approves and copied by the registry like the code. It declares {"reads": [...], "network": []}.

Enforcement is by visibility, not by trust: the host builds a view folder that holds copies of only
the declared files and the sandbox mounts that view read-only as /inputs. A tool that opens anything
undeclared finds nothing there. No sandbox flag changes; the view is just a different inputs folder.

Network: the sandbox has no network and that is not negotiable here. A manifest that lists any domain
is rejected, never honoured. Allowing domains would need a host-side proxy, which is a separate piece.

What this does not do: it does not limit what a tool does with the files it declared, and a broad
declaration is still a broad declaration. The human reads the manifest in the diff for that reason.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 4_096
MAX_READS = 10
MAX_DEPTH = 3
MAX_VIEW_BYTES = 10 * 1024 * 1024

_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_KEYS = frozenset({"reads", "network"})


class ManifestError(ValueError):
    """A manifest is malformed, asks for something that cannot be granted, or cannot be enforced safely."""


@dataclass(frozen=True)
class Manifest:
    reads: tuple[str, ...]
    network: tuple[str, ...] = ()  # always empty: the sandbox has no network


@dataclass(frozen=True)
class View:
    path: Path  # what to mount as /inputs
    missing: tuple[str, ...]  # declared files that were not in the source folder


def _check_path(path: object) -> str:
    if not isinstance(path, str):
        raise ManifestError("each entry in 'reads' must be a string")
    if "\x00" in path or "\\" in path:
        raise ManifestError(f"unsafe file name {path[:40]!r}")
    parts = path.split("/")
    if len(parts) > MAX_DEPTH or not all(_SEGMENT.fullmatch(p) for p in parts):
        raise ManifestError(
            f"file name {path[:40]!r} must be a relative path of plain names (letters, digits, '.', '_', '-'), at most {MAX_DEPTH} deep"
        )
    return path


def parse_manifest(text: str) -> Manifest:
    """Turn untrusted manifest text into a Manifest. Anything unexpected raises ManifestError."""
    if len(text.encode("utf-8", errors="replace")) > MAX_MANIFEST_BYTES:
        raise ManifestError(f"manifest is longer than {MAX_MANIFEST_BYTES} bytes")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise ManifestError(f"unknown manifest keys: {', '.join(map(str, unknown))[:60]}")
    if "reads" not in data:
        raise ManifestError("manifest must declare 'reads' (use [] for a tool that reads nothing)")
    reads = data["reads"]
    if not isinstance(reads, list):
        raise ManifestError("'reads' must be a list")
    if len(reads) > MAX_READS:
        raise ManifestError(f"at most {MAX_READS} files in 'reads'")
    checked = [_check_path(r) for r in reads]
    if len(set(checked)) != len(checked):
        raise ManifestError("'reads' lists the same file twice")
    network = data.get("network", [])
    if not isinstance(network, list):
        raise ManifestError("'network' must be a list")
    if network:
        raise ManifestError(
            f"network: domains are not supported because the sandbox has no network (got {str(network[0])[:40]!r}); list none"
        )
    return Manifest(reads=tuple(checked))


def load_manifest(tool_dir: Path) -> Manifest | None:
    """The manifest in a tool folder, or None when there is none. A bad one raises ManifestError."""
    path = tool_dir / MANIFEST_NAME
    if path.is_symlink():
        raise ManifestError(f"{MANIFEST_NAME} must not be a symlink")
    if not path.exists():
        return None
    if not path.is_file():
        raise ManifestError(f"{MANIFEST_NAME} must be a regular file")
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ManifestError(f"manifest is longer than {MAX_MANIFEST_BYTES} bytes")
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError("manifest is not UTF-8 text") from None
    return parse_manifest(text)


def describe(manifest: Manifest) -> str:
    """One readable line for the reviewer."""
    reads = ", ".join(manifest.reads) if manifest.reads else "nothing"
    return f"reads {reads}; network none"


@contextmanager
def input_view(manifest: Manifest | None, source: Path) -> Iterator[View]:
    """Yield the folder to mount as /inputs: `source` itself with no manifest, else a copy of only the declared files.

    The view is a copy, so nothing the sandbox does can reach the real inputs, and it is removed on exit.
    Symlinks are never followed (at any path component), folders are not files, and the total size is capped.
    """
    if manifest is None:
        yield View(source, ())
        return
    root = Path(tempfile.mkdtemp(prefix="forge-view-"))
    try:
        root.chmod(0o755)  # the sandbox user is unprivileged and must be able to read it
        real_source = source.resolve()
        missing: list[str] = []
        total = 0
        for rel in manifest.reads:
            src = source
            for part in rel.split("/"):
                src = src / part
                if src.is_symlink():
                    raise ManifestError(f"{rel}: symlinks are not followed")
            if not src.exists():
                missing.append(rel)
                continue
            if not src.is_file():
                raise ManifestError(f"{rel} is not a regular file")
            if real_source not in src.resolve().parents:
                raise ManifestError(f"{rel} is outside the inputs folder")
            total += src.stat().st_size
            if total > MAX_VIEW_BYTES:
                raise ManifestError(f"declared files are larger than {MAX_VIEW_BYTES} bytes in total")
            current = root
            for part in rel.split("/")[:-1]:
                current = current / part
                current.mkdir(exist_ok=True)
                current.chmod(0o755)
            dest = root / rel
            shutil.copyfile(src, dest)
            dest.chmod(0o644)
        yield View(root, tuple(missing))
    finally:
        shutil.rmtree(root, ignore_errors=True)
