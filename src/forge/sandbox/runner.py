"""Run one command in a throwaway sandbox container and capture bounded output.

SECURITY-CRITICAL. Calls the docker CLI via subprocess. No Docker SDK.
"""

from __future__ import annotations

import subprocess
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO

from forge.sandbox.policy import SandboxPolicy, build_docker_args

_CHUNK = 64 * 1024
_CLEANUP_TIMEOUT_S = 30


class SandboxUnavailable(RuntimeError):
    """The docker CLI is missing, so no sandbox run is possible."""


@dataclass(frozen=True)
class SandboxResult:
    container_name: str
    exit_code: int | None  # None when the run was killed by the timeout
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool


class _BoundedReader(threading.Thread):
    """Keeps at most `limit` bytes and drains the rest, so a noisy tool cannot fill host memory."""

    def __init__(self, stream: IO[bytes], limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._buf = bytearray()
        self.truncated = False

    def run(self) -> None:
        while chunk := self._stream.read(_CHUNK):
            room = self._limit - len(self._buf)
            if room > 0:
                self._buf.extend(chunk[:room])
            if len(chunk) > max(room, 0):
                self.truncated = True
        self._stream.close()

    @property
    def text(self) -> str:
        return self._buf.decode("utf-8", errors="replace")


def _force_remove(container_name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        timeout=_CLEANUP_TIMEOUT_S,
        check=False,
    )


def run_in_sandbox(policy: SandboxPolicy, command: Sequence[str]) -> SandboxResult:
    """Run `command` in a fresh container built from `policy`. Always removes the container."""
    name = f"forge-{uuid.uuid4().hex[:12]}"
    argv = build_docker_args(policy, container_name=name, command=list(command))

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SandboxUnavailable("docker CLI not found on PATH") from exc

    assert proc.stdout is not None and proc.stderr is not None
    out = _BoundedReader(proc.stdout, policy.max_output_bytes)
    err = _BoundedReader(proc.stderr, policy.max_output_bytes)
    out.start()
    err.start()

    timed_out = False
    try:
        exit_code: int | None = proc.wait(timeout=policy.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = None
        _force_remove(name)
        proc.kill()
        proc.wait()

    out.join()
    err.join()
    _force_remove(name)  # no-op on the normal path (--rm already removed it)

    return SandboxResult(
        container_name=name,
        exit_code=exit_code,
        stdout=out.text,
        stderr=err.text,
        timed_out=timed_out,
        stdout_truncated=out.truncated,
        stderr_truncated=err.truncated,
    )
