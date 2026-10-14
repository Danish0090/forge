"""Unit tests for the runner that never start a container. No Docker daemon needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.sandbox.policy import SandboxPolicy
from forge.sandbox.runner import SandboxUnavailable, run_in_sandbox

DIGEST = "python@sha256:" + "a" * 64


def test_missing_docker_cli_raises_sandbox_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    policy = SandboxPolicy(image=DIGEST, tool_dir=tool_dir)
    empty_path = tmp_path / "empty-bin"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    with pytest.raises(SandboxUnavailable):
        run_in_sandbox(policy, ["python", "-c", "print(1)"])
