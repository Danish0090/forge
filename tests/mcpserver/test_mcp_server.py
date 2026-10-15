"""MCP server tests. No Docker: the sandbox is faked. Skipped when the optional `mcp` extra is not installed.

The server is host-side. It exposes only tools that are already in the registry (verified, approved), runs
every call through the orchestrator's existing path (hash re-check, manifest view, sandbox) and offers no way
to forge or approve anything, so an MCP client can use approved tools but never create one.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from forge.approval.gate import ApprovalDecision  # noqa: E402
from forge.checks.hash_pin import tree_hash  # noqa: E402
from forge.mcpserver import ForgeMcp  # noqa: E402
from forge.mcpserver.__main__ import main  # noqa: E402
from forge.orchestrator import orchestrator as orch_mod  # noqa: E402
from forge.registry import Registry  # noqa: E402
from forge.sandbox.policy import SandboxPolicy  # noqa: E402
from forge.sandbox.runner import SandboxResult  # noqa: E402

DIGEST = "python@sha256:" + "a" * 64
DESC = 'Counts words in a text file. Args: {"path": str}'


def register(registry: Registry, tmp_path: Path, name: str = "word_count", body: str = "print(42)\n", manifest: dict | None = None, desc: str = DESC):
    d = tmp_path / f"draft_{name}"
    d.mkdir()
    (d / "tool.py").write_text(body)
    if manifest is not None:
        (d / "manifest.json").write_text(json.dumps(manifest))
    return registry.register(name, d, desc, ApprovalDecision(True, "approved", approved_sha256=tree_hash(d)))


class FakeSandbox:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.result = SandboxResult("c", 0, "42\n", "", False, False, False)

    def __call__(self, policy: SandboxPolicy, command: list[str]) -> SandboxResult:
        mounted = policy.inputs_dir
        self.calls.append({
            "command": list(command), "tool_dir": policy.tool_dir, "inputs": mounted,
            "files": sorted(p.name for p in mounted.rglob("*") if p.is_file()),
        })
        return self.result


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> FakeSandbox:
    fake = FakeSandbox()
    monkeypatch.setattr(orch_mod, "run_in_sandbox", fake)
    return fake


@pytest.fixture
def inputs(tmp_path: Path) -> Path:
    d = tmp_path / "inputs"
    d.mkdir()
    (d / "notes.txt").write_text("one two\n")
    (d / "secret.txt").write_text("TOP SECRET\n")
    return d


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "registry")


def server(registry: Registry, inputs: Path, **kw: object) -> ForgeMcp:
    return ForgeMcp(registry, image=DIGEST, inputs_dir=inputs, **kw)  # type: ignore[arg-type]


def names(forge: ForgeMcp) -> list[str]:
    async def go() -> list[str]:
        async with Client(forge.mcp) as c:
            return sorted(t.name for t in await c.list_tools())

    return asyncio.run(go())


def call(forge: ForgeMcp, name: str, args: object = None):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        async with Client(forge.mcp) as c:
            return await c.call_tool(name, {} if args is None else {"args": args})

    return asyncio.run(go())


# --- what is exposed ------------------------------------------------------------------------------------


def test_exactly_the_registered_tools_are_exposed(tmp_path: Path, registry: Registry, inputs: Path) -> None:
    register(registry, tmp_path, "word_count")
    register(registry, tmp_path, "line_count")
    assert names(server(registry, inputs)) == ["line_count", "word_count"]


def test_an_empty_registry_exposes_nothing_so_no_forge_or_approve_tool_exists(tmp_path: Path, registry: Registry, inputs: Path) -> None:
    assert names(server(registry, inputs)) == []


def test_descriptions_carry_the_registry_text_and_warn_that_output_is_untrusted(tmp_path: Path, registry: Registry, inputs: Path) -> None:
    entry = register(registry, tmp_path)
    forge = server(registry, inputs)

    async def go():  # type: ignore[no-untyped-def]
        async with Client(forge.mcp) as c:
            return (await c.list_tools())[0].description

    text = asyncio.run(go())
    assert DESC in text and "untrusted" in text.lower() and entry.sha256[:12] in text


def test_the_server_tells_clients_what_these_tools_are(tmp_path: Path, registry: Registry, inputs: Path) -> None:
    text = (server(registry, inputs).mcp.instructions or "").lower()
    assert "sandbox" in text and "approved" in text and "untrusted" in text


def test_a_tampered_tool_is_not_listed(tmp_path: Path, registry: Registry, inputs: Path) -> None:
    entry = register(registry, tmp_path)
    (entry.path / "tool.py").write_text("print('owned')\n")
    assert names(server(registry, inputs)) == []


# --- calls ----------------------------------------------------------------------------------------------


def test_a_call_runs_in_the_sandbox_and_returns_the_output(tmp_path: Path, registry: Registry, inputs: Path, sandbox: FakeSandbox) -> None:
    entry = register(registry, tmp_path)
    result = call(server(registry, inputs), "word_count", {"path": "/inputs/notes.txt"})
    assert result.content[0].text == "42\n"
    (run,) = sandbox.calls
    assert run["tool_dir"] == entry.path  # the registry's verified copy, never the original folder
    assert run["command"] == ["python", "/app/tool.py", json.dumps({"path": "/inputs/notes.txt"})]  # one JSON argv element


def test_no_arguments_means_an_empty_object(tmp_path: Path, registry: Registry, inputs: Path, sandbox: FakeSandbox) -> None:
    register(registry, tmp_path)
    call(server(registry, inputs), "word_count")
    assert sandbox.calls[0]["command"][-1] == "{}"


def test_a_failing_tool_is_a_tool_error_with_the_reason(tmp_path: Path, registry: Registry, inputs: Path, sandbox: FakeSandbox) -> None:
    register(registry, tmp_path)
    sandbox.result = replace(sandbox.result, exit_code=1, stdout="", stderr="Traceback: boom")
    with pytest.raises(ToolError) as exc:
        call(server(registry, inputs), "word_count")
    assert "exited with code 1" in str(exc.value) and "boom" in str(exc.value)


def test_a_timeout_is_a_tool_error(tmp_path: Path, registry: Registry, inputs: Path, sandbox: FakeSandbox) -> None:
    register(registry, tmp_path)
    sandbox.result = replace(sandbox.result, exit_code=None, timed_out=True)
    with pytest.raises(ToolError) as exc:
        call(server(registry, inputs), "word_count")
    assert "timed out" in str(exc.value)


def test_output_is_capped(tmp_path: Path, registry: Registry, inputs: Path, sandbox: FakeSandbox) -> None:
    register(registry, tmp_path)
    sandbox.result = replace(sandbox.result, stdout="x" * 50_000)
    text = call(server(registry, inputs), "word_count").content[0].text
    assert len(text) <= orch_mod.MAX_OBSERVATION_CHARS + 1


def test_oversize_arguments_are_refused_before_the_sandbox(tmp_path: Path, registry: Registry, inputs: Path, sandbox: FakeSandbox) -> None:
    register(registry, tmp_path)
    with pytest.raises(ToolError):
        call(server(registry, inputs), "word_count", {"blob": "x" * (orch_mod.MAX_ARGS_BYTES + 1)})
    assert sandbox.calls == []


@pytest.mark.parametrize("bad", ["text", [1, 2], 5])
def test_arguments_must_be_an_object(tmp_path: Path, registry: Registry, inputs: Path, sandbox: FakeSandbox, bad: object) -> None:
    register(registry, tmp_path)
    with pytest.raises(Exception):
        call(server(registry, inputs), "word_count", bad)
    assert sandbox.calls == []


def test_an_unknown_tool_cannot_be_called(tmp_path: Path, registry: Registry, inputs: Path, sandbox: FakeSandbox) -> None:
    with pytest.raises(ToolError):
        call(server(registry, inputs), "forge_new_tool")
    assert sandbox.calls == []


# --- the registry stays the authority ---------------------------------------------------------------------


def test_a_tool_edited_after_listing_is_refused_by_the_hash_check(tmp_path: Path, registry: Registry, inputs: Path, sandbox: FakeSandbox) -> None:
    entry = register(registry, tmp_path)
    forge = server(registry, inputs)
    (entry.path / "tool.py").write_text("print('owned')\n")
    with pytest.raises(ToolError) as exc:
        call(forge, "word_count")
    assert "failed hash verification" in str(exc.value)
    assert sandbox.calls == []


def test_a_revoked_tool_is_refused_even_while_still_listed(tmp_path: Path, registry: Registry, inputs: Path, sandbox: FakeSandbox) -> None:
    register(registry, tmp_path)
    forge = server(registry, inputs)
    registry.revoke("word_count")
    with pytest.raises(ToolError):
        call(forge, "word_count")
    assert sandbox.calls == []


def test_sync_exposes_tools_approved_after_the_server_started(tmp_path: Path, registry: Registry, inputs: Path) -> None:
    register(registry, tmp_path, "word_count")
    forge = server(registry, inputs)
    register(registry, tmp_path, "line_count")
    assert names(forge) == ["word_count"]
    assert forge.sync() == ["line_count"]
    assert names(forge) == ["line_count", "word_count"]
    assert forge.sync() == []  # nothing new the second time


# --- the manifest still applies over MCP ---------------------------------------------------------------------


def test_a_tool_with_a_manifest_sees_only_its_declared_files(tmp_path: Path, registry: Registry, inputs: Path, sandbox: FakeSandbox) -> None:
    register(registry, tmp_path, manifest={"reads": ["notes.txt"], "network": []})
    call(server(registry, inputs), "word_count")
    assert sandbox.calls[0]["files"] == ["notes.txt"] and sandbox.calls[0]["inputs"] != inputs


def test_a_required_manifest_refuses_a_tool_without_one(tmp_path: Path, registry: Registry, inputs: Path, sandbox: FakeSandbox) -> None:
    register(registry, tmp_path)
    with pytest.raises(ToolError) as exc:
        call(server(registry, inputs, require_manifest=True), "word_count")
    assert "no manifest" in str(exc.value)
    assert sandbox.calls == []


# --- command line ---------------------------------------------------------------------------------------------


def test_cli_lists_the_tools_it_would_serve(tmp_path: Path, registry: Registry, inputs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    register(registry, tmp_path)
    monkeypatch.setenv("FORGE_SANDBOX_IMAGE", DIGEST)
    monkeypatch.setattr("forge.mcpserver.__main__.docker_ready", lambda: True)
    assert main(["--registry", str(tmp_path / "registry"), "--inputs", str(inputs), "--list"]) == 0
    assert "word_count" in capsys.readouterr().out


def test_cli_needs_the_image_docker_and_an_existing_registry(tmp_path: Path, inputs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = ["--registry", str(tmp_path / "registry"), "--inputs", str(inputs), "--list"]
    monkeypatch.delenv("FORGE_SANDBOX_IMAGE", raising=False)
    assert main(args) == 2
    monkeypatch.setenv("FORGE_SANDBOX_IMAGE", DIGEST)
    monkeypatch.setattr("forge.mcpserver.__main__.docker_ready", lambda: False)
    assert main(args) == 2
    monkeypatch.setattr("forge.mcpserver.__main__.docker_ready", lambda: True)
    assert main(args) == 2  # the registry folder does not exist, and serving must not create one
    assert not (tmp_path / "registry").exists()


def test_cli_refuses_missing_inputs(tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_SANDBOX_IMAGE", DIGEST)
    monkeypatch.setattr("forge.mcpserver.__main__.docker_ready", lambda: True)
    assert main(["--registry", str(tmp_path / "registry"), "--inputs", str(tmp_path / "nope"), "--list"]) == 2


# --- end to end in a real container ------------------------------------------------------------------------------


@pytest.mark.docker
def test_an_mcp_client_calls_a_forged_tool_in_a_real_container(tmp_path: Path, sandbox_image: str) -> None:
    from forge.bench import benchmark
    from forge.bench.tasks import TASKS

    task = next(t for t in TASKS if t.id == "word-count")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name, text in task.files.items():
        (inputs / name).write_text(text)
    reg = Registry(tmp_path / "registry")
    planner, forger = benchmark.oracle_factory(task, sorted(task.files))
    run = orch_mod.Orchestrator(
        registry=reg, planner=planner, forger=forger, image=sandbox_image, inputs_dir=inputs,
        workdir=tmp_path / "work", ask=lambda _p: "yes", show=lambda _t: None, require_manifest=True,
    ).run(task.prompt)
    assert run.forged == ("word_count",)

    forge = ForgeMcp(reg, image=sandbox_image, inputs_dir=inputs, require_manifest=True)
    assert names(forge) == ["word_count"]
    assert call(forge, "word_count", {"path": "/inputs/notes.txt"}).content[0].text.strip() == "18"
    # a path outside the declared files is simply not there
    with pytest.raises(ToolError):
        call(forge, "word_count", {"path": "/inputs/other.txt"})


@pytest.mark.docker
def test_a_real_mcp_client_talks_to_the_cli_over_stdio(tmp_path: Path, sandbox_image: str) -> None:
    """The real process, the real protocol, a real container: stdout must carry only MCP messages."""
    import os
    import sys

    from fastmcp.client.transports import StdioTransport

    from forge.bench import benchmark
    from forge.bench.tasks import TASKS

    task = next(t for t in TASKS if t.id == "word-count")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for name, text in task.files.items():
        (inputs / name).write_text(text)
    reg = Registry(tmp_path / "registry")
    planner, forger = benchmark.oracle_factory(task, sorted(task.files))
    orch_mod.Orchestrator(
        registry=reg, planner=planner, forger=forger, image=sandbox_image, inputs_dir=inputs,
        workdir=tmp_path / "work", ask=lambda _p: "yes", show=lambda _t: None,
    ).run(task.prompt)

    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "forge.mcpserver", "--registry", str(tmp_path / "registry"), "--inputs", str(inputs), "--timeout", "20"],
        env={**os.environ, "FORGE_SANDBOX_IMAGE": sandbox_image},
    )

    async def go():  # type: ignore[no-untyped-def]
        async with Client(transport) as c:
            listed = sorted(t.name for t in await c.list_tools())
            result = await c.call_tool("word_count", {"args": {"path": "/inputs/notes.txt"}})
            return listed, result.content[0].text.strip()

    assert asyncio.run(go()) == (["word_count"], "18")
