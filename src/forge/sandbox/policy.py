"""Sandbox policy: the hardening config for every generated-tool run.

SECURITY-CRITICAL. Changes here need the project owner's review.
Hardening flags are fixed in build_docker_args and are not configurable.
Only resource limits are tunable, and they are validated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SANDBOX_USER = "65534:65534"  # nobody:nogroup
SCRATCH_DIR = "/scratch"
CODE_DIR = "/app"
INPUTS_DIR = "/inputs"  # optional read-only mount, used by the staging stage
NOFILE_LIMIT = 256  # open file descriptors per run

_DIGEST_REF = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
_MEMORY = re.compile(r"^[1-9][0-9]*[bkmg]$")


class PolicyError(ValueError):
    """Raised when a sandbox policy is invalid. Invalid policies never run."""


@dataclass(frozen=True)
class SandboxPolicy:
    image: str
    tool_dir: Path
    memory: str = "256m"
    cpus: float = 0.5
    pids_limit: int = 64
    timeout_s: float = 10.0
    scratch_size: str = "64m"
    max_output_bytes: int = 65_536
    inputs_dir: Path | None = None

    def __post_init__(self) -> None:
        if not _DIGEST_REF.match(self.image):
            raise PolicyError(f"image must be pinned by digest (name@sha256:...), got {self.image!r}")
        if "," in str(self.tool_dir):
            # The path is spliced into a comma-separated --mount option; a comma would inject options.
            raise PolicyError(f"tool_dir must not contain a comma: {self.tool_dir}")
        if not self.tool_dir.is_absolute():
            raise PolicyError(f"tool_dir must be absolute: {self.tool_dir}")
        if not self.tool_dir.is_dir():
            raise PolicyError(f"tool_dir does not exist: {self.tool_dir}")
        if self.inputs_dir is not None:
            if "," in str(self.inputs_dir):
                raise PolicyError(f"inputs_dir must not contain a comma: {self.inputs_dir}")
            if not self.inputs_dir.is_absolute():
                raise PolicyError(f"inputs_dir must be absolute: {self.inputs_dir}")
            if not self.inputs_dir.is_dir():
                raise PolicyError(f"inputs_dir does not exist: {self.inputs_dir}")
            if self.inputs_dir.resolve() == self.tool_dir.resolve():
                raise PolicyError("inputs_dir must not be the tool_dir")
        if not _MEMORY.match(self.memory):
            raise PolicyError(f"memory must look like 256m or 1g, got {self.memory!r}")
        if not _MEMORY.match(self.scratch_size):
            raise PolicyError(f"scratch_size must look like 64m, got {self.scratch_size!r}")
        if self.cpus <= 0:
            raise PolicyError("cpus must be positive")
        if self.pids_limit <= 0:
            raise PolicyError("pids_limit must be positive")
        if self.timeout_s <= 0:
            raise PolicyError("timeout_s must be positive")
        if self.max_output_bytes <= 0:
            raise PolicyError("max_output_bytes must be positive")


def build_docker_args(
    policy: SandboxPolicy,
    *,
    container_name: str,
    command: list[str],
) -> list[str]:
    """Return the full `docker run` argv for one throwaway sandbox run."""
    inputs_mount = (
        [
            "--mount",
            f"type=bind,source={policy.inputs_dir},target={INPUTS_DIR},readonly",
        ]
        if policy.inputs_dir is not None
        else []
    )
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--user",
        SANDBOX_USER,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        policy.memory,
        "--memory-swap",
        policy.memory,
        "--cpus",
        str(policy.cpus),
        "--pids-limit",
        str(policy.pids_limit),
        "--ulimit",
        f"nofile={NOFILE_LIMIT}:{NOFILE_LIMIT}",
        "--tmpfs",
        f"{SCRATCH_DIR}:rw,noexec,nosuid,nodev,size={policy.scratch_size},mode=1777",
        "--workdir",
        SCRATCH_DIR,
        "--mount",
        f"type=bind,source={policy.tool_dir},target={CODE_DIR},readonly",
        *inputs_mount,
        policy.image,
        *command,
    ]
