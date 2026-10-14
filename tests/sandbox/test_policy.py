"""Unit tests for sandbox policy. No Docker daemon needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.sandbox.policy import (
    PolicyError,
    SandboxPolicy,
    build_docker_args,
)

DIGEST = "python@sha256:" + "a" * 64


@pytest.fixture
def tool_dir(tmp_path: Path) -> Path:
    d = tmp_path / "tool"
    d.mkdir()
    return d


@pytest.fixture
def policy(tool_dir: Path) -> SandboxPolicy:
    return SandboxPolicy(image=DIGEST, tool_dir=tool_dir)


def args_for(policy: SandboxPolicy) -> list[str]:
    return build_docker_args(policy, container_name="forge-test-1", command=["pytest", "-q"])


def test_required_hardening_flags_present(policy: SandboxPolicy) -> None:
    args = args_for(policy)
    for flag in (
        "--network",
        "--read-only",
        "--cap-drop",
        "--security-opt",
        "--rm",
        "--memory",
        "--pids-limit",
        "--cpus",
        "--user",
    ):
        assert flag in args, f"missing {flag}"
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"


def test_runs_as_non_root(policy: SandboxPolicy) -> None:
    args = args_for(policy)
    user = args[args.index("--user") + 1]
    assert user.split(":")[0] not in ("0", "root")


def test_scratch_is_the_only_writable_path(policy: SandboxPolicy) -> None:
    args = args_for(policy)
    tmpfs_specs = [args[i + 1] for i, a in enumerate(args) if a == "--tmpfs"]
    assert len(tmpfs_specs) == 1
    assert tmpfs_specs[0].startswith("/scratch")
    assert "noexec" in tmpfs_specs[0]
    assert "nosuid" in tmpfs_specs[0]
    assert "--workdir" in args and args[args.index("--workdir") + 1] == "/scratch"


def test_tool_code_is_mounted_read_only(policy: SandboxPolicy, tool_dir: Path) -> None:
    args = args_for(policy)
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert len(mounts) == 1
    assert f"source={tool_dir}" in mounts[0]
    assert "target=/app" in mounts[0]
    assert "readonly" in mounts[0]


def test_no_escape_hatches(policy: SandboxPolicy) -> None:
    args = args_for(policy)
    forbidden = {"--privileged", "--env", "-e", "--env-file", "-v", "--volume", "--ipc", "--pid"}
    assert not forbidden & set(args)
    joined = " ".join(args)
    assert "docker.sock" not in joined
    assert "network host" not in joined
    assert "--network=host" not in joined
    assert "--network bridge" not in joined


def test_image_must_be_pinned_by_digest(tool_dir: Path) -> None:
    with pytest.raises(PolicyError):
        SandboxPolicy(image="python:3.12-slim", tool_dir=tool_dir)


def test_malformed_digest_rejected(tool_dir: Path) -> None:
    with pytest.raises(PolicyError):
        SandboxPolicy(image="python@sha256:abc", tool_dir=tool_dir)


@pytest.mark.parametrize(
    "field, value",
    [
        ("cpus", 0),
        ("cpus", -1),
        ("pids_limit", 0),
        ("timeout_s", 0),
        ("max_output_bytes", 0),
    ],
)
def test_non_positive_limits_rejected(tool_dir: Path, field: str, value: float) -> None:
    kwargs = {"image": DIGEST, "tool_dir": tool_dir, field: value}
    with pytest.raises(PolicyError):
        SandboxPolicy(**kwargs)


@pytest.mark.parametrize("memory", ["", "lots", "256 m", "-1m"])
def test_bad_memory_format_rejected(tool_dir: Path, memory: str) -> None:
    with pytest.raises(PolicyError):
        SandboxPolicy(image=DIGEST, tool_dir=tool_dir, memory=memory)


def test_tool_dir_must_exist_and_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        SandboxPolicy(image=DIGEST, tool_dir=tmp_path / "missing")
    with pytest.raises(PolicyError):
        SandboxPolicy(image=DIGEST, tool_dir=Path("relative/dir"))


def test_memory_swap_disabled(policy: SandboxPolicy) -> None:
    args = args_for(policy)
    memory = args[args.index("--memory") + 1]
    assert args[args.index("--memory-swap") + 1] == memory


def test_command_is_last_and_image_precedes_it(policy: SandboxPolicy) -> None:
    args = args_for(policy)
    assert args[-2:] == ["pytest", "-q"]
    assert args[-3] == DIGEST


def test_run_never_pulls_from_the_registry(policy: SandboxPolicy) -> None:
    args = args_for(policy)
    assert args[args.index("--pull") + 1] == "never"


def test_open_file_limit_is_set(policy: SandboxPolicy) -> None:
    args = args_for(policy)
    ulimits = [args[i + 1] for i, a in enumerate(args) if a == "--ulimit"]
    assert any(u.startswith("nofile=") for u in ulimits)


def test_tool_dir_with_comma_rejected(tmp_path: Path) -> None:
    # A comma would split the --mount option string and let the path inject options.
    tricky = tmp_path / "tool,readonly=false"
    tricky.mkdir()
    with pytest.raises(PolicyError):
        SandboxPolicy(image=DIGEST, tool_dir=tricky)
