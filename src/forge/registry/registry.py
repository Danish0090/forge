"""Registry: the only place the orchestrator may load tools from.

SECURITY-CRITICAL. Changes here need the project owner's review.

A tool enters only with an approved decision whose hash matches the files being registered.
The registry re-runs the checks itself and stores its own copy of the files, so later edits to
the source folder cannot change what was approved. Every read re-hashes the stored copy; a tool
that no longer matches its approved hash is never returned. Revoked tools are never returned.

Known limit: the registry cannot prove an ApprovalDecision came from the real gate (it is a
plain dataclass). The caller must pass the decision request_approval just returned. Someone who
can write to the registry directory can replace both a tool and its hash; protect that folder.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from forge.approval.gate import ApprovalDecision
from forge.checks import run_checks
from forge.checks.hash_pin import tree_hash

_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_SKIP = shutil.ignore_patterns("__pycache__")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (name, version),
    UNIQUE (name, sha256)
)
"""


class RegistryError(Exception):
    """A tool was refused, is missing, or failed verification."""


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    version: int
    sha256: str
    description: str
    path: Path  # the registry's own copy, never the original folder


class Registry:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._tools = self._root / "tools"
        self._tools.mkdir(parents=True, exist_ok=True)
        self._db_path = self._root / "registry.db"
        with self._connect() as db:
            db.execute(_SCHEMA)

    def register(
        self, name: str, tool_dir: Path, description: str, decision: ApprovalDecision
    ) -> RegisteredTool:
        """Store an approved tool as the new active version of `name`."""
        if not _NAME_RE.fullmatch(name):
            raise RegistryError(f"invalid tool name: {name!r}")
        if not decision.approved or not decision.approved_sha256:
            raise RegistryError("tool not approved")
        if any(p.is_symlink() for p in [tool_dir, *tool_dir.rglob("*")]):
            raise RegistryError("tool folder contains a symlink")
        if tree_hash(tool_dir) != decision.approved_sha256:
            raise RegistryError("tool files changed since approval")
        sha = decision.approved_sha256
        report = run_checks(tool_dir, description, sha)
        if not report.passed:
            names = ", ".join(sorted({f.check for f in report.findings}))
            raise RegistryError(f"checks failed: {names}")

        final = self._dir(name, sha)
        if final.exists():
            raise RegistryError(f"{name} with this hash is already registered")
        final.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=self._tools, prefix=".incoming-"))
        try:
            copy = staging / "tool"
            shutil.copytree(tool_dir, copy, ignore=_SKIP)
            if tree_hash(copy) != sha:
                raise RegistryError("tool files changed while being copied")
            copy.rename(final)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        shutil.rmtree(staging, ignore_errors=True)

        try:
            with self._connect() as db:
                version = db.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM tools WHERE name = ?", (name,)
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO tools (name, version, sha256, description, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (name, version, sha, description, datetime.now(UTC).isoformat()),
                )
        except sqlite3.Error as exc:
            shutil.rmtree(final, ignore_errors=True)
            raise RegistryError(f"could not record {name} (concurrent registration?): {exc}") from exc
        except BaseException:
            shutil.rmtree(final, ignore_errors=True)
            raise
        return RegisteredTool(name, version, sha, description, final)

    def get(self, name: str) -> RegisteredTool:
        """The active version of `name`, verified against its approved hash."""
        if not _NAME_RE.fullmatch(name):
            raise RegistryError(f"invalid tool name: {name!r}")
        with self._connect() as db:
            row = db.execute(
                "SELECT version, sha256, description FROM tools"
                " WHERE name = ? AND revoked = 0 ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
        if row is None:
            raise RegistryError(f"{name} is not in the registry")
        version, sha, description = row
        if not _SHA_RE.fullmatch(sha):
            raise RegistryError(f"{name} failed hash verification")
        path = self._dir(name, sha)
        if not path.is_dir() or tree_hash(path) != sha:
            raise RegistryError(f"{name} failed hash verification")
        return RegisteredTool(name, version, sha, description, path)

    def list_tools(self) -> list[RegisteredTool]:
        """Active, verified tools. Tampered tools are left out, not returned."""
        with self._connect() as db:
            names = [r[0] for r in db.execute("SELECT DISTINCT name FROM tools ORDER BY name")]
        found: list[RegisteredTool] = []
        for name in names:
            try:
                found.append(self.get(name))
            except RegistryError:
                continue
        return found

    def failing(self) -> list[str]:
        """Names of active tools that fail verification, so tampering is reported, not hidden."""
        with self._connect() as db:
            names = [r[0] for r in db.execute("SELECT DISTINCT name FROM tools ORDER BY name")]
        bad: list[str] = []
        for name in names:
            try:
                self.get(name)
            except RegistryError as exc:
                if "failed hash verification" in str(exc):
                    bad.append(name)
        return bad

    def revoke(self, name: str) -> None:
        """Withdraw every version of `name`. Files stay on disk for inspection."""
        with self._connect() as db:
            cur = db.execute("UPDATE tools SET revoked = 1 WHERE name = ? AND revoked = 0", (name,))
        if cur.rowcount == 0:
            raise RegistryError(f"{name} is not in the registry")

    def _dir(self, name: str, sha: str) -> Path:
        return self._tools / name / sha

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._db_path)
        try:
            with db:  # commit on success, roll back on error
                yield db
        finally:
            db.close()
