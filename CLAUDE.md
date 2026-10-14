# CLAUDE.md

Guidance for Claude Code when working in this repo.

## Project

`forge` tests one claim: an AI agent can safely extend its own capabilities if every new tool must earn trust before it runs.

Flow: orchestrator detects a capability gap -> forge agent writes a new MCP server (FastMCP) plus tests -> the tool climbs the trust ladder -> only approved tools enter the registry -> orchestrator reloads them. A benchmark and an attack suite measure reliability and safety per model.

Personal learning and open-source project. Weekend build.

## Trust ladder (one stage per session)

1. **Sandbox** (done): Docker, no network, non-root, scratch folder only, CPU/memory limits, throwaway container per run.
2. **Checks**: static scan, prompt-injection scan of tool descriptions, hash pin to detect later changes.
3. **Staging**: run on real but read-only inputs.
4. **Approval**: CLI shows a diff, a human says yes or no.

"Earn trust" means evidence (passing tests, clean scans, human approval), never the agent's own claim.

## Hard rules

- Generated tools run ONLY inside the Docker sandbox.
- Never put API keys or credentials inside the sandbox.
- Never weaken a sandbox, check, or approval gate without asking the project owner first.
- Synthetic data only. No employer code, data, or terminology anywhere in this repo.
- Tests first for every module. Run `pytest` before saying a task is done.
- One trust-ladder stage per session. Small commits, one stage per commit.
- Ask before adding dependencies or making network calls from the host (this includes `docker pull`).
- The project owner personally reviews security-critical code: `src/forge/sandbox/`, the approval gate, and the checks.
- Report negative results honestly.

## Commands

    uv run pytest                                                   # unit tests, no Docker
    FORGE_SANDBOX_IMAGE='python@sha256:<digest>' uv run pytest      # full suite, needs Docker

Docker tests skip when `FORGE_SANDBOX_IMAGE` is unset. The approved image is `python:3.12-slim`, pinned by digest (see README).

## Stack and conventions

- Python 3.12, managed with uv. Type hints everywhere.
- FastMCP, LiteLLM (or OpenRouter, OpenAI-compatible), Docker, OpenTelemetry, SQLite, pytest.
- Models: Kimi K2, MiniMax M2, Claude. Switched by one config line. Model API calls happen OUTSIDE the sandbox.
- Security-critical modules start with a `SECURITY-CRITICAL` note in their docstring. Hardening flags are fixed in code, not configurable. Only resource limits are tunable, and they are validated.
- Sandbox runs call the `docker` CLI through `subprocess`. No Docker SDK.

## Layout

    src/forge/sandbox/   policy.py (flags + validation), runner.py (runs one container)
    tests/sandbox/       test_policy.py (no Docker), test_runner_unit.py (no Docker),
                         test_runner.py (real containers, marked `docker`)
    tests/conftest.py    skips Docker tests unless the daemon and image are present

## Status

- Sandbox stage: implemented and tested. Commit `68b454b`, plus uncommitted hardening (`--pull never`, nofile ulimit, comma guard in `tool_dir`).
- Checks stage: implemented and tested, uncommitted (`src/forge/checks/`, `tests/checks/`). Static scan (AST), injection scan (heuristic), hash pin (SHA-256 of the tool folder), and a pipeline that reports all findings.
- Not built: staging, approval, registry, orchestrator, model router, benchmark, attack suite.

## Git

- Commit as the repo's configured git user. Do NOT add `Co-Authored-By` or other AI attribution lines to commits or PR descriptions.
- Do not commit or push unless asked.
