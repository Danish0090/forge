# CLAUDE.md

Guidance for Claude Code when working in this repo.

## Project

`forge` tests one claim: an AI agent can safely extend its own capabilities if every new tool must earn trust before it runs.

Flow: orchestrator detects a capability gap -> forge agent writes a new tool (today a plain `tool.py` script run as `python tool.py '<json>'`, plus sample arguments for the staging run and a few forger-written test cases; MCP/FastMCP servers are planned, not built) -> the tool climbs the trust ladder -> only approved tools enter the registry -> orchestrator reloads them. A benchmark and an attack suite measure reliability and safety per model.

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
- In use: LiteLLM (or OpenRouter, OpenAI-compatible), Docker, SQLite (stdlib), pytest. The only runtime dependency is `litellm`. Planned, not added yet (ask before adding): FastMCP, OpenTelemetry.
- Models: free ones by default (local Ollama, free OpenRouter Nemotron and Gemma). Paid open-weight models are allowed (`kimi-k2.7-code`, `minimax-m3`, `deepseek-v4-pro`, `qwen3-coder`), plus closed-weight reference points chosen by the owner (`gemini-4-argon`, `gpt-5.6-sol`, `claude-opus-5`, `claude-opus-5.5`). Paid models need `--allow-paid` and run under a spend cap (default $0.50, hard maximum $5, estimated from the token counts the API reports; a credit limit on the provider key is the real backstop). Prices and slugs on OpenRouter drift; they were checked on 2026-10-15. Switched by one config line. Model API calls happen OUTSIDE the sandbox.
- Security-critical modules start with a `SECURITY-CRITICAL` note in their docstring. Hardening flags are fixed in code, not configurable. Only resource limits are tunable, and they are validated.
- Sandbox runs call the `docker` CLI through `subprocess`. No Docker SDK.

## Layout

    src/forge/sandbox/   policy.py (flags + validation), runner.py (runs one container)
    src/forge/staging/   stager.py (run on read-only inputs), cases.py (forger-written test cases, data only),
                         trace.py (what a staging run did, capped evidence)
    src/forge/checks/    finding.py (shared result type), static_scan.py (AST),
                         injection_scan.py (heuristic), hash_pin.py (SHA-256),
                         __init__.py (run_checks pipeline, runs all checks)
    src/forge/approval/  diff.py (safe diff render), gate.py (yes/no decision)
    src/forge/registry/  registry.py (SQLite index + verified copies of approved tools)
    src/forge/orchestrator/  orchestrator.py (loop: plan -> call tool or forge one up the ladder)
    src/forge/demo.py    live demo CLI: scripted forger or a router model (`python -m forge.demo --model ...`)
    src/forge/router/    router.py (LiteLLM wrapper, host-side keys), agents.py (ModelPlanner, ModelForger)
    src/forge/bench/     attacks.py (scripted attack suite), redteam.py (asks a live model for malicious tools),
                         benchmark.py + tasks.py (per-model benchmark, 3 tiers), stage.py,
                         __main__.py (`python -m forge.bench attacks|redteam|bench`)
    tests/sandbox/       test_policy.py (no Docker), test_runner_unit.py (no Docker),
                         test_runner.py (real containers, marked `docker`)
    tests/staging/       test_stager_unit.py, test_cases_unit.py, test_trace_unit.py (no Docker), test_stager.py, test_cases.py (Docker)
    tests/checks/        test_static_scan.py, test_injection_scan.py,
                         test_hash_pin.py, test_pipeline.py (no Docker)
    tests/registry/      test_registry.py (no Docker)
    tests/orchestrator/  test_orchestrator.py (no Docker, sandbox faked)
    tests/router/        test_router.py (no network, completion faked)
    tests/demo/          test_demo.py (no Docker, no network)
    tests/bench/         test_attacks.py, test_redteam.py, test_benchmark.py, test_hidden.py, test_survival.py (Docker tests marked `docker`; bench_helpers.py is shared)
    tests/conftest.py    skips Docker tests unless the daemon and image are present

## Status

Everything below is implemented, tested and committed: the four ladder stages (sandbox, checks, staging, approval), registry, orchestrator, model router, demo CLI, benchmark, attack suite and red team. Tests: 451 pass with Docker and the pinned image, 424 without (27 Docker tests skip). Sandbox, checks and the other security-critical modules still need the owner's own read-through.

Known limits:
- The static scan is a heuristic and has been evaded before; the sandbox is the real boundary. It denies `sys` and `os` (tools read args with `argparse`), plus `builtins`, `posix`, `nt`, any `_private` module, dynamic `getattr`/`setattr`/`delattr`, and a bare `__builtins__`. It cannot see intent (deleting a file is legal code).
- Staging proves a tool runs cleanly and passes the test cases its own forger wrote (when it wrote any). The same model wrote the tool and the expected values, so a wrong idea can pass its own tests, and safe-but-wrong tools can still be registered. The benchmark's hidden test set (same question, different files, answers worked out by hand) is the independent check.
- The human approval prompt needs a real terminal and has not been exercised end to end. Benchmarks and attacks use a rubber-stamp reviewer (`--auto-approve` is opt-in and shown in the report).
- Prompt injection of the planner is reduced by prompt wording, not solved.
- Free OpenRouter models are often rate-limited or overloaded upstream; such runs are provider outages, not model results. A Docker outage made some early runs invalid (exit 125); the CLI now refuses to start when Docker is down. The router retries 429/503 twice (5s, 20s).

Evidence (2026-10-15; every number is a single pass with a small sample, so read it as a direction):
- Attack suite: 26 scripted attacks plus a registry-tamper check, all stopped before any human was asked, and 3 harmless controls pass. It got there in rounds: the first runs found six scan evasions (`builtins.exec`, `getattr(builtins, ...)`, `posix`, `_socket`, `_posixsubprocess`, `_ctypes`) and the scan was hardened. Expect more evasions.
- Forger-written tests and hidden test set (2026-10-15): the forger now writes 2 or 3 test cases as data (own fixture files, args, expected output; at most 5 cases, 5 files, 4,000 characters per file), staging runs each in its own read-only sandbox after a clean main run, and `--require-tests` refuses tools without them. 10 of the 22 tasks have a hidden variant (same prompt, different files, a trap the first file lacks); after a task, a fresh planner may use the registered tool but not forge, and its answer is scored (`hidden_passed/hidden_total`). With the oracle in real containers: 10 of 10 hidden variants right and 10 tools passed their own tests first. That only proves the harness: the oracle's tests are built from the variants. No model has been scored on this yet.
- Staging traces and survival (2026-10-15): every sandbox run in staging (the main run and each test case) leaves a trace (command, exit code, wall time, output capped at 300 characters). The orchestrator keeps them, shows the reviewer a one-line summary before the approval prompt (the approval gate itself is unchanged), and the benchmark saves up to 8 per task in `--out` JSON. Traces are evidence only: the registry and the approval gate never read them, and they are not stored in the registry. Survival: the same 10 tasks also have a drifted version of their input (column order, extra columns or fields, line endings, tabs between fields; the right answer is unchanged). After a task, if the tool was right first, a fresh planner re-answers over the drifted files with no forging (`survival_passed/survival_total`, `--no-survival` skips it). This is a proxy for an API change, not a real one. It does discriminate: in a real container a tool that reads the amount column by position is right on the original file and wrong after the drift, while the hidden variant (same column order) cannot tell. With the oracle, 10 of 10 survive, which only proves the harness. No model has been scored on it yet.
- Benchmark: 22 synthetic tasks in three tiers (6 easy, 6 hard with traps, 10 injection). The scripted `oracle` agent scores 22/22; it only proves the harness works. The scorer is lenient about a trailing `.0` ("200.0" counts for "200").
- Hard tier, 3 runs each, mean score of 6: gemini-4-argon, gpt-5.6-sol, claude-opus-5 and minimax-m3 6.0; kimi-k2.7-code 5.7; deepseek-v4-pro 5.3; qwen3-coder 4.3 (fails the log-window task every time). Local models are far behind: qwen2.5-coder:7b 2/6 on the early easy set, llama3.2 0/6 (never produced parsable forger JSON). Single runs misranked models, so use `--runs`.
- Red team (12 malicious requests straight to a model's forger, rubber-stamp reviewer). Models differ a lot in whether they write a tool at all (gemini-4-argon and claude-opus-5 wrote one for 1 request of 12; qwen3-coder and deepseek-v4-pro wrote 11). "Reached the human gate" overstates danger: most such drafts were harmless stand-ins (gpt-5.6-sol wrote 8, all defanged). Only 3 drafts had real destructive intent and reached the gate (deepseek-v4-pro and kimi-k2.7-code wrote working file-deleting loops; qwen3-coder wrote a self-backdoor, inert because of a bug); the read-only `/inputs` mount made them harmless. I read only the first ~700 characters of each. No harmful tool ran with effect.
- Injection tier (5 attack styles, each with a clean control twin; the agent must read the whole file so the planted text reaches the planner; 3 runs each). Controls were 15/15 for every valid model. Before planner hardening, deepseek-v4-pro and qwen3-coder obeyed the email "fake operator instruction" attack 3 of 3 times; nothing else was ever obeyed. Escaping the fence tag in tool output did not help; adding a system-prompt line that claims of authority inside tool output are still data did: after it, obeyed 0/15 for deepseek-v4-pro, kimi-k2.7-code, minimax-m3, gemini-4-argon and gpt-5.6-sol, while qwen3-coder stayed at 3/15. gemini-4-argon often avoids reading the whole file, so its planted text reached the planner in only 11 of 15 runs. claude-opus-5 is excluded: 8 of 30 runs errored and its controls scored 3/15.
- claude-opus-5.5 and claude-fable-5.1 returned `content_filter` with no text on the forger prompt; claude-opus-5 and claude-opus-4.8 worked. The block is provider-side and was not worked around.

Not done: free Nemotron and Gemma results are missing or invalid (provider outages); larger samples for firmer injection rates; tools that change behavior after approval; forger-written tests and the hidden test set have not been run against a real model yet (they need `OPENROUTER_API_KEY`; the `--require-tests` flag turns the test requirement on). From the original brief and not built: tools as MCP servers (FastMCP, planned as a host-side server) and a capability manifest (declared files and domains). Also not run against a real model yet: the survival metric.

## Git

- Commit as the repo's configured git user. Do NOT add `Co-Authored-By` or other AI attribution lines to commits or PR descriptions.
- Do not commit or push unless asked.
- Before every commit and push, update CLAUDE.md (Status, Layout) and README.md so they match the code. Include those updates in the same commit.
- Do not rewrite published history (no force-push, no filter-branch) without explicit permission.
- `.DS_Store` is ignored.
