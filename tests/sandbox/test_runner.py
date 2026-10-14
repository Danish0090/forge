"""Integration tests: these start real containers. Need Docker and FORGE_SANDBOX_IMAGE."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from forge.sandbox.policy import SandboxPolicy, build_docker_args
from forge.sandbox.runner import run_in_sandbox

pytestmark = pytest.mark.docker

PROBE_NETWORK = (
    "import socket\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
    "    print('REACHED')\n"
    "except OSError:\n"
    "    print('BLOCKED')\n"
)


@pytest.fixture
def tool_dir(tmp_path: Path) -> Path:
    d = tmp_path / "tool"
    d.mkdir()
    (d / "tool.py").write_text("print('hello')\n")
    return d


@pytest.fixture
def policy(tool_dir: Path, sandbox_image: str) -> SandboxPolicy:
    return SandboxPolicy(image=sandbox_image, tool_dir=tool_dir, timeout_s=20)


def py(code: str) -> list[str]:
    return ["python", "-c", code]


def test_network_is_blocked(policy: SandboxPolicy) -> None:
    result = run_in_sandbox(policy, py(PROBE_NETWORK))
    assert result.stdout.strip() == "BLOCKED"


def test_dns_lookup_fails(policy: SandboxPolicy) -> None:
    code = (
        "import socket\n"
        "try:\n"
        "    socket.gethostbyname('example.com')\n"
        "    print('RESOLVED')\n"
        "except OSError:\n"
        "    print('BLOCKED')\n"
    )
    result = run_in_sandbox(policy, py(code))
    assert result.stdout.strip() == "BLOCKED"


def test_write_outside_scratch_fails(policy: SandboxPolicy) -> None:
    code = (
        "for path in ('/etc/forge-probe', '/app/forge-probe'):\n"
        "    try:\n"
        "        open(path, 'w').write('x')\n"
        "        print('WROTE', path)\n"
        "    except OSError:\n"
        "        print('BLOCKED', path)\n"
    )
    result = run_in_sandbox(policy, py(code))
    assert "WROTE" not in result.stdout
    assert result.stdout.count("BLOCKED") == 2


def test_host_files_outside_mounts_are_invisible(policy: SandboxPolicy, tmp_path: Path) -> None:
    secret = tmp_path / "host-secret.txt"
    secret.write_text("do not leak")
    result = run_in_sandbox(policy, py(f"import os; print(os.path.exists({str(secret)!r}))"))
    assert result.stdout.strip() == "False"


def test_scratch_is_writable(policy: SandboxPolicy) -> None:
    code = "open('/scratch/ok.txt', 'w').write('x'); print('OK')"
    result = run_in_sandbox(policy, py(code))
    assert result.stdout.strip() == "OK"


def test_scratch_is_ephemeral(policy: SandboxPolicy) -> None:
    run_in_sandbox(policy, py("open('/scratch/marker.txt', 'w').write('x')"))
    result = run_in_sandbox(policy, py("import os; print(os.path.exists('/scratch/marker.txt'))"))
    assert result.stdout.strip() == "False"


def test_runs_as_non_root(policy: SandboxPolicy) -> None:
    result = run_in_sandbox(policy, py("import os; print(os.getuid())"))
    assert result.stdout.strip() != "0"


def test_cannot_gain_privileges_or_raw_sockets(policy: SandboxPolicy) -> None:
    code = (
        "import os, socket\n"
        "try:\n"
        "    os.setuid(0); print('ROOT')\n"
        "except PermissionError:\n"
        "    print('NO_SETUID')\n"
        "try:\n"
        "    socket.socket(socket.AF_INET, socket.SOCK_RAW, 1); print('RAW')\n"
        "except PermissionError:\n"
        "    print('NO_RAW')\n"
    )
    result = run_in_sandbox(policy, py(code))
    lines = result.stdout.split()
    assert "ROOT" not in lines
    assert "RAW" not in lines
    assert "NO_SETUID" in lines
    assert "NO_RAW" in lines


def test_fork_bomb_is_contained(tool_dir: Path, sandbox_image: str) -> None:
    policy = SandboxPolicy(
        image=sandbox_image, tool_dir=tool_dir, pids_limit=32, timeout_s=20
    )
    code = (
        "import os, time\n"
        "ok = 0\n"
        "for _ in range(200):\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "    except OSError:\n"
        "        break\n"
        "    if pid == 0:\n"
        "        time.sleep(30)\n"
        "        os._exit(0)\n"
        "    ok += 1\n"
        "print('FORKS', ok)\n"
    )
    result = run_in_sandbox(policy, py(code))
    assert not result.timed_out
    forks = int(result.stdout.split()[-1])
    assert forks < 200


def test_memory_hog_is_killed(tool_dir: Path, sandbox_image: str) -> None:
    policy = SandboxPolicy(image=sandbox_image, tool_dir=tool_dir, memory="64m", timeout_s=30)
    code = "x = []\nwhile True:\n    x.append(b'a' * 1024 * 1024)\n"
    result = run_in_sandbox(policy, py(code))
    assert result.exit_code == 137
    assert "DONE" not in result.stdout


def test_cpu_spin_times_out_and_container_is_removed(tool_dir: Path, sandbox_image: str) -> None:
    policy = SandboxPolicy(image=sandbox_image, tool_dir=tool_dir, timeout_s=3)
    started = time.monotonic()
    result = run_in_sandbox(policy, py("while True:\n    pass\n"))
    elapsed = time.monotonic() - started
    assert result.timed_out
    assert elapsed < 15
    assert not container_exists(result.container_name)


def test_containers_are_removed_after_normal_exit(policy: SandboxPolicy) -> None:
    result = run_in_sandbox(policy, py("print('done')"))
    assert result.exit_code == 0
    assert not container_exists(result.container_name)


def test_host_secrets_are_not_passed_in(policy: SandboxPolicy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-do-not-leak")
    result = run_in_sandbox(policy, ["env"])
    assert "OPENROUTER" not in result.stdout
    assert "sk-test-do-not-leak" not in result.stdout


def test_output_is_capped(tool_dir: Path, sandbox_image: str) -> None:
    policy = SandboxPolicy(
        image=sandbox_image, tool_dir=tool_dir, max_output_bytes=1024, timeout_s=20
    )
    result = run_in_sandbox(policy, py("print('a' * 1_000_000)"))
    assert len(result.stdout) <= 1024
    assert result.stdout_truncated


def test_meta_weakened_policy_is_detected(tool_dir: Path, sandbox_image: str) -> None:
    """If the network probe passes under a weakened policy, the probe is real."""
    policy = SandboxPolicy(image=sandbox_image, tool_dir=tool_dir, timeout_s=20)
    args = build_docker_args(policy, container_name="forge-meta-probe", command=py(PROBE_NETWORK))
    weakened = [a if a != "none" else "bridge" for a in args]
    proc = subprocess.run(weakened, capture_output=True, text=True, timeout=60, check=False)
    assert proc.stdout.strip() == "REACHED", (
        "probe did not reach the network under a weakened policy; "
        "the network test cannot be trusted"
    )


def container_exists(name: str) -> bool:
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return name in out.stdout.split()
