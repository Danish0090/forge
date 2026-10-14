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
- Models (free only for now): local Ollama models and free OpenRouter open-weight models (Nemotron, Gemma). Kimi, MiniMax, DeepSeek, Qwen and Claude are paid-only for this owner. Switched by one config line. Model API calls happen OUTSIDE the sandbox.
- Security-critical modules start with a `SECURITY-CRITICAL` note in their docstring. Hardening flags are fixed in code, not configurable. Only resource limits are tunable, and they are validated.
- Sandbox runs call the `docker` CLI through `subprocess`. No Docker SDK.

## Layout

    src/forge/sandbox/   policy.py (flags + validation), runner.py (runs one container)
    src/forge/checks/    finding.py (shared result type), static_scan.py (AST),
                         injection_scan.py (heuristic), hash_pin.py (SHA-256),
                         __init__.py (run_checks pipeline, runs all checks)
    src/forge/approval/  diff.py (safe diff render), gate.py (yes/no decision)
    src/forge/registry/  registry.py (SQLite index + verified copies of approved tools)
    src/forge/orchestrator/  orchestrator.py (loop: plan -> call tool or forge one up the ladder)
    src/forge/demo.py    live demo CLI: scripted forger or a router model (`python -m forge.demo --model ...`)
    src/forge/router/    router.py (LiteLLM wrapper, host-side keys), agents.py (ModelPlanner, ModelForger)
    src/forge/bench/     attacks.py (attack suite), benchmark.py + tasks.py (per-model benchmark),
                         stage.py, __main__.py (`python -m forge.bench attacks|bench`)
    tests/sandbox/       test_policy.py (no Docker), test_runner_unit.py (no Docker),
                         test_runner.py (real containers, marked `docker`)
    tests/checks/        test_static_scan.py, test_injection_scan.py,
                         test_hash_pin.py, test_pipeline.py (no Docker)
    tests/registry/      test_registry.py (no Docker)
    tests/orchestrator/  test_orchestrator.py (no Docker, sandbox faked)
    tests/router/        test_router.py (no network, completion faked)
    tests/demo/          test_demo.py (no Docker, no network)
    tests/bench/         test_attacks.py, test_benchmark.py (Docker tests marked `docker`)
    tests/conftest.py    skips Docker tests unless the daemon and image are present

## Status

- Sandbox stage: implemented, tested, committed. Staging adds an optional read-only `inputs_dir` mount at `/inputs` (needs owner review).
- Checks stage: implemented, tested, committed (`src/forge/checks/`).
- Staging stage: implemented, tested, committed (`src/forge/staging/`, `tests/staging/`).
- Approval stage: implemented, tested, committed (`src/forge/approval/`, `tests/approval/`).
- Static scan host-safety hardening (size cap, symlink flag, parser-crash findings): implemented, tested, committed.
- Registry: implemented, tested, committed (`src/forge/registry/`, `tests/registry/`).
- Orchestrator: implemented, tested, committed (`src/forge/orchestrator/`, `tests/orchestrator/`). Planner and Forger are Protocols.
- Model router: implemented, tested with a fake backend, uncommitted (`src/forge/router/`, `tests/router/`). LiteLLM added with owner approval; host network calls to model APIs approved. Open models supported: Ollama (`ollama/<tag>`, localhost, no key) and OpenRouter free aliases only (nemotron, nemotron-ultra, nemotron-nano, nemotron-lightning, gemma, gemma-26b; slugs checked 2026-10-14, they drift). Live runs so far: qwen2.5-coder:7b via Ollama forged 0 of 2 tool attempts (ladder blocked both); the free OpenRouter Qwen slug 404'd.
- Demo CLI: implemented, tested with fakes; good scripted tool verified in a real container (staging only; the human approval prompt needs a real terminal and has not been exercised end to end).
- Known limit: the static scan denies `sys` and `os`, so tools must read args via `argparse` (the forger prompt says so).
- Live results so far: qwen2.5-coder:7b (Ollama) 0 of 2 forges; nemotron (OpenRouter free) 0 of 2 (syntax errors, and staging_args that did not match the tool). Forger now self-checks syntax, retries, and is told the input file names.
- Benchmark and attack suite: implemented, tested, committed (`src/forge/bench/`, `tests/bench/`). Attack suite: 27 scripted attacks plus 3 controls plus a registry-tamper check, run through the real ladder with a rubber-stamp reviewer; all outcomes match expectations in real containers. Findings: the first run showed 3 scan-evading attacks reaching the human (`builtins.exec`, `getattr(builtins, ...)`, `posix`), a second round 3 more (`_socket`, `_posixsubprocess`, `_ctypes`); the static scan was hardened (denies `builtins`, `posix`, `nt`, any `_private` module, dynamic `getattr`/`setattr`/`delattr`, bare `__builtins__`) with owner approval, and now all 27 attacks stop before a human. Expect further evasions. Benchmark: 6 synthetic tasks (one with a planted instruction); the `oracle` agent (scripted, not a model) scores 6/6 and proves the harness. Live benchmark, qwen2.5-coder:7b (Ollama, 2026-10-15): 0/6 at first, because the planner looped on the same tool without finishing; after planner prompt fixes (finish once a tool answers; make `need` an exact computation) 2/6. Remaining failures: forger writes generic or buggy tools, uses `import os` (checks stop it), or crashes in staging. llama3.2: 0/6, never produced parsable forger JSON. Nothing unsafe was ever registered; safe-but-wrong tools were (staging cannot judge correctness). Results are nondeterministic run to run. Each task result keeps failure reasons, draft code and a step trace in the `--out` JSON. `--auto-approve` is opt-in and recorded in the report.
- Not built: benchmark results for real models; attacks that need a live model (tool-output injection of the planner).

## Git

- Commit as the repo's configured git user. Do NOT add `Co-Authored-By` or other AI attribution lines to commits or PR descriptions.
- Do not commit or push unless asked.
- Before every commit and push, update CLAUDE.md (Status, Layout) and README.md so they match the code. Include those updates in the same commit.
- Do not rewrite published history (no force-push, no filter-branch) without explicit permission.
- `.DS_Store` is ignored.
