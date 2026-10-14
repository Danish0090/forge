"""Shared fixtures. Docker tests skip unless the daemon and a pinned image are present."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


@pytest.fixture(scope="session")
def sandbox_image() -> str:
    image = os.environ.get("FORGE_SANDBOX_IMAGE", "")
    if not image:
        pytest.skip("set FORGE_SANDBOX_IMAGE to a digest-pinned image to run docker tests")
    if not _docker_available():
        pytest.skip("Docker daemon is not reachable")
    return image
