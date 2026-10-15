# forge

**Thesis:** an AI agent can safely extend its own capabilities if every new tool must *earn trust* before it runs.

Status: **All four ladder stages (sandbox, checks, staging, approval) are done**, along with the registry, the orchestrator, the model router, a benchmark, an attack suite and a red team. They have been run against real models; the results and their limits are in [CLAUDE.md](CLAUDE.md).

Source code: https://github.com/Danish0090/forge

This README explains what the project is, what has been built so far, and why each piece exists. Read it top to bottom once, then use the "Run the tests" section as a reference.

## The idea in one paragraph

Imagine an AI assistant that notices it's missing a tool, say a log parser. Instead of asking a human to write one, it writes the tool itself. That is useful and also dangerous: the tool is code written by a model, and the model can be wrong, confused, or manipulated by text it reads. So forge never runs a new tool directly. A new tool has to pass a series of gates, the **trust ladder**, and each gate needs *evidence*: passing tests, clean scans, or a human's yes. The agent's own claim that "this tool is safe" never counts.

## The trust ladder

| Stage | Question it answers | Status |
|---|---|---|
| 1. Sandbox | Can the tool hurt the machine when it runs? | **Done** |
| 2. Checks | Does the code or its description look malicious or changed? | **Done** (heuristic, see below) |
| 3. Staging | Does it behave on real but read-only inputs? | **Done** |
| 4. Approval | Does a human, reading a diff, say yes? | **Done** |

Only a tool that clears all four enters the registry, and only then does the orchestrator load it.

## Stage 1: the sandbox (what exists now)

Every run of a generated tool happens in a **throwaway Docker container**. The container is created for one command and deleted afterward. The settings are in [src/forge/sandbox/policy.py](src/forge/sandbox/policy.py). Each flag exists to block one kind of harm:

| Setting | What it does | Why |
|---|---|---|
| `--network none` | No network interface | A tool can't send data out or call an API. |
| `--read-only` | Root filesystem is read-only | A tool can't modify the image. |
| `--tmpfs /scratch` | One writable folder, in memory, no execute | The only place a tool can write. It vanishes on exit. |
| `--user 65534` | Runs as `nobody`, not root | Limits what a compromised process can do. |
| `--cap-drop ALL` | Removes all Linux capabilities | No raw sockets, no privileged syscalls. |
| `no-new-privileges` | Blocks setuid escalation | A process can't gain root later. |
| `--memory`, `--cpus`, `--pids-limit` | Resource caps | A runaway loop or fork bomb can't take the host down. |
| `--mount ... readonly` | Tool code mounted read-only at `/app` | The tool can't rewrite its own code. |
| `--pull never` | Never downloads an image during a run | Every run is offline with respect to the registry. |
| `--ulimit nofile` | Caps open file descriptors | Limits resource abuse. |
| Timeout | Kills the run after N seconds | Stops infinite loops. |
| Output cap | Keeps at most 64 KB of stdout and stderr | A noisy tool can't fill host memory. |
| No env passed | Host environment variables are not forwarded | API keys on the host never reach the sandbox. |

The image must be **pinned by digest** (`name@sha256:...`), not by a tag like `:latest`. A tag can change under you; a digest cannot. The approved image is `python:3.12-slim`:

    python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

### How the code is tested

- **Policy tests** ([tests/sandbox/test_policy.py](tests/sandbox/test_policy.py)) check the generated `docker run` command without running anything. They confirm each flag is present and that bad input is rejected.
- **Runner unit tests** ([tests/sandbox/test_runner_unit.py](tests/sandbox/test_runner_unit.py)) check behavior like "docker is missing" without starting a container.
- **Docker tests** ([tests/sandbox/test_runner.py](tests/sandbox/test_runner.py)) start real containers and try to break out: reach the network, write outside `/scratch`, read host files, escalate to root, fork-bomb, exhaust memory, spin forever, and leak a fake API key. They also check a **meta-test**: the same probe run under a deliberately weakened policy *must* succeed. If it doesn't, the network test proves nothing.

## Stage 2: checks (what exists now)

Checks run before a tool is allowed anywhere near staging. They read the tool; they never run it. All three run every time, so a reviewer sees every problem at once:

- **Static scan** ([src/forge/checks/static_scan.py](src/forge/checks/static_scan.py)): parses the code with Python's `ast` module and flags imports of risky modules (`socket`, `subprocess`, `os`, ...), calls to `eval`/`exec`, and attribute chains used in sandbox-escape tricks (`__subclasses__`, `__globals__`). It also protects the host: files over 256 KB and symlinked `.py` files are flagged without being read, and parser crashes (extreme nesting, null bytes) become findings instead of exceptions.
- **Injection scan** ([src/forge/checks/injection_scan.py](src/forge/checks/injection_scan.py)): looks at the tool's description for phrases like "ignore previous instructions" and for invisible Unicode characters that hide text from a reviewer.
- **Hash pin** ([src/forge/checks/hash_pin.py](src/forge/checks/hash_pin.py)): a SHA-256 over every file in the tool folder. Once a tool is approved, any change to its files changes the hash and fails the check.

Honest limits: a static scan can be dodged by a determined author (string tricks, indirect imports), and the injection scan only knows phrasings it was written for. These checks catch cheap, common cases early. The sandbox is still the real boundary.

## Stage 3: staging (what exists now)

Staging runs the tool once in the same sandbox, with a folder of real inputs mounted read-only at `/inputs` ([src/forge/staging/stager.py](src/forge/staging/stager.py)). The report fails on a non-zero exit, a timeout, truncated output, or any change to the input or tool files (hashed before and after). The mount is read-only, so a hash change would mean the boundary failed. Staging only proves the tool behaves on those inputs; it is not proof of general correctness.

**Forger-written tests.** The forger also writes 2 or 3 test cases ([src/forge/staging/cases.py](src/forge/staging/cases.py)). A case is data, not code: a few tiny fixture files, the arguments to call the tool with, and the exact output it should print. After a clean main run, staging runs each case in its own throwaway sandbox with its own fixtures mounted read-only, and compares the output. A failing case stops the tool before any human is asked. Limits: at most 5 cases, 5 files per case and 4,000 characters per file, plain file names only. The same model wrote the tool and the expected values, so a wrong idea can pass its own tests. That is why the benchmark has a hidden test set (below). Turn the requirement on with `--require-tests`; without it, tools with no tests are still accepted.

**Capability manifest.** A tool can declare the input files it reads in `manifest.json` inside its folder: `{"reads": ["notes.txt"], "network": []}` ([src/forge/staging/manifest.py](src/forge/staging/manifest.py)). The file is part of the tool folder, so it is hashed, pinned, shown in the diff you approve and copied by the registry like the code. Nothing in `sandbox/`, `checks/`, `approval/` or `registry/` changed. Enforcement is by visibility: the host builds a view with copies of only the declared files and mounts that read-only as `/inputs`, in staging, in every test case and in every later call. An undeclared file is simply not there. Symlinks are never followed, folders are refused, and there are limits: 10 files, 3 levels deep, 10 MB. The network stays off: a manifest that lists a domain is rejected, never honoured, because real domain allow-lists would need a host-side proxy. A bad or missing manifest stops in checks; a declared file that is not in the inputs stops staging. It is off by default; `--require-manifest` makes it mandatory. It does not limit what a tool does with the files it declared, so read the manifest in the diff.

**Traces.** Each sandbox run in staging (the main run and every test case) also leaves a trace ([src/forge/staging/trace.py](src/forge/staging/trace.py)): the command, exit code, wall time and the first 300 characters of output. The orchestrator keeps them, shows the reviewer a one-line summary before the approval prompt, and the benchmark saves up to 8 per task in its `--out` JSON. A trace is evidence only. The approval gate and the registry never read it, and it is not stored in the registry.

## Stage 4: approval (what exists now)

The gate ([src/forge/approval/gate.py](src/forge/approval/gate.py)) shows a human the tool's hash, its description, and a unified diff ([diff.py](src/forge/approval/diff.py)) against the previous version (or all-added for a new tool). Rules:

- The human is only asked when the evidence is clean. The gate re-runs the checks itself and needs a passing staging report. Failed evidence cannot be overridden by a yes.
- Only an explicit `yes` approves. Empty input, `y`, EOF, Ctrl-C, or no terminal (pipe, cron, an agent) all reject.
- Terminal escapes, carriage returns, bidi overrides and zero-width characters are escaped in the diff and description, so nothing can hide from the reviewer. The diff covers exactly the files the hash pin covers.
- A diff over 2000 lines is rejected rather than truncated.
- The tool folder is hashed again after the answer; a change while the human was reading rejects.

Known limit: the staging report is not cryptographically bound to the tool it ran on, so the caller must pass the report from the run it just did. The registry stage should store the approved hash and re-verify it on load.

## The registry (what exists now)

The registry ([src/forge/registry/registry.py](src/forge/registry/registry.py)) is the only place the orchestrator may load tools from. It keeps an SQLite index and its own copy of each approved tool's files. Rules:

- A tool enters only with an approved decision whose hash matches the files being registered. The registry also re-runs the checks itself.
- It stores a copy, so editing the source folder later cannot change what was approved.
- Every `get` re-hashes the stored copy. A tool that no longer matches its approved hash is refused, and `list_tools` leaves it out.
- Tool names are restricted to `[a-z][a-z0-9_]{0,63}`; stored hashes must be 64 hex characters, so a tampered database row cannot point outside the registry folder.
- A tool folder containing any symlink is refused, so a link cannot pull a host file into the registry.
- `failing()` reports active tools that no longer match their hash; `list_tools` only leaves them out.
- Re-registering a name creates a new version; the latest unrevoked one is active. `revoke` withdraws a tool but keeps its files for inspection.

Known limits: the registry cannot prove a decision came from the real gate (it is a plain dataclass), and someone who can write to the registry folder can replace both a tool and its hash. Protect that folder.

## The orchestrator (what exists now)

The orchestrator ([src/forge/orchestrator/orchestrator.py](src/forge/orchestrator/orchestrator.py)) is the loop. Each step it reloads the registry, asks a **planner** for the next action, and does one of three things: call an approved tool, report the answer, or hit a capability gap. On a gap it asks the **forger** for a draft tool and takes it up the ladder in fixed order: checks, staging in the sandbox, human approval, registry. Only then can the planner call it.

- The planner and forger are interfaces (`Protocol`s). Both are model-backed (see below), so their output is treated as untrusted data. Orchestrator tests use scripted ones.
- Tools run only from the registry (re-hashed on every load) and only in the sandbox. Arguments go in as one JSON argv element, capped at 8 KB. The stored copy is hashed again after the run, and output is discarded if it changed.
- Every failure (forger crash, failed checks, failed staging, human no, tampered tool, bad call) becomes an observation the planner sees. Failed evidence never reaches the human.
- Limits: at most 10 steps and 2 forge attempts per run (both configurable). Tool output is length-capped, but it is still untrusted text; the planner prompt must treat it as data.

## The model router (what exists now)

The router ([src/forge/router/](src/forge/router/)) is the only code that calls a model, and it runs on the host, never in the sandbox. One config value picks the model (LiteLLM underneath). No paid key is needed: use `ollama/<tag>` for any model pulled into local Ollama (for example `ollama/qwen2.5-coder:7b`; calls go to `localhost:11434` only, no key), or paid open-weight models (see below), or the free open-weight OpenRouter aliases `nemotron`, `nemotron-ultra`, `nemotron-nano`, `nemotron-lightning`, `gemma` and `gemma-26b` (need a free `OPENROUTER_API_KEY`). Free tiers change: as of 2026-10-14 Qwen, DeepSeek, Kimi and MiniMax are paid-only there, so they are not offered.

- API keys, when a model needs one, come from host environment variables (`OPENROUTER_API_KEY`), are passed per call, and are scrubbed from error messages. They never enter a container.
- `ModelPlanner` and `ModelForger` parse replies strictly (one JSON object, validated fields) and raise `RouterError` on anything else. The forger only writes `tool.py` into a fresh folder; the ladder decides whether it ever runs. Tool output reaches the planner inside `<untrusted_output>` tags (any copy of the tag inside the output is escaped) with a system prompt that says to treat it as data, even when the text claims to come from the system, an operator or the orchestrator.
- Reliability aids inside the (untrusted) forger only: low temperature, the list of files under `/inputs` in the prompt, and up to 3 drafts when its own `ast.parse` check fails. None of this touches a gate; the ladder still judges every draft.
- Unverified: the OpenRouter slugs were checked against its public list on 2026-10-14 and drift (a `:free` Qwen slug already 404'd) and the Ollama tag must already be pulled by you and no live call has been made. Tests use a fake completion function, so `pytest` makes no network calls.

## Benchmark and attack suite (what exists now)

Code: [src/forge/bench/](src/forge/bench/). Both run the real ladder with real containers.

    uv run python -m forge.bench attacks                        # 34 attacks, 4 controls and 2 tamper checks (the report counts the tamper checks too: "36 of 36")
    uv run python -m forge.bench bench --model oracle --auto-approve
    uv run python -m forge.bench bench --model qwen3-coder --allow-paid --tier hard --runs 3   # pass counts and spread
    uv run python -m forge.bench bench --model kimi-k2.7-code --allow-paid --tier hard --runs 3 --require-tests --require-manifest --max-tokens 12000
    uv run python -m forge.bench bench --model ollama/qwen2.5-coder:7b   # asks you at each approval

- **Attack suite** ([attacks.py](src/forge/bench/attacks.py)): hostile or broken tools (network, subprocess, eval, sandbox-escape attributes, injection in the description, hidden Unicode, oversize or symlinked files, writes to read-only paths, infinite loops, memory bombs, output floods) plus harmless controls that must *not* be blocked. The human is replaced by a rubber stamp, the worst case, so an attack counts as stopped only if it never reaches the human. Result: all 34 attacks and both tamper checks stopped automatically (plus 4 harmless controls that pass) after the scan was hardened. Eight of the attacks target the capability manifest (see staging). The saved report is [results/attacks.txt](results/attacks.txt). The first run found that 3 attacks evaded the scan, reached the human gate and looked fine in staging (`builtins.exec`, `getattr(builtins, ...)`, `posix.system`); a follow-up round found 3 more (`_socket`, `_posixsubprocess`, `_ctypes`). The scan now also denies `builtins`, `posix`, `nt` and any `_private` module, `getattr`/`setattr`/`delattr` unless the name is a plain non-dunder string literal, and a bare `__builtins__`. Expect more evasions: the scan is a heuristic and the sandbox is the real boundary. A final check edits an approved tool's stored copy and confirms the next call is refused.
- **Benchmark** (22 tasks in three tiers: easy, hard and injection; `--tier hard` runs only the traps) ([benchmark.py](src/forge/bench/benchmark.py), [tasks.py](src/forge/bench/tasks.py)): small tasks over synthetic files, each with a fresh registry. It records correctness, forge attempts, and the stage where each failed attempt stopped (a model that keeps writing rejected code is less safe even if every attempt is caught). One task hides an instruction in an input file to see whether the planner obeys it. Each result also keeps the reason every failed forge attempt was stopped the draft code the model wrote, and the step-by-step trace of the run (in the `--out` JSON), so a failure can be diagnosed. The `oracle` agent uses hand-written tools, is not a model, and scores 6/6, which only shows the harness works. Live so far (2026-10-15; the unique-users and poisoned-heading prompts were clarified after a first pass, so older scores on them are not comparable): `qwen3-coder`, `deepseek-v4-pro`, `kimi-k2.7-code` and `minimax-m3` each 6 of 6 (cost under $0.02 per run). The easy tier is saturated, so a hard tier was added: a join with an orphan row, a messy money column, a log time window where the word ERROR appears in other lines, a latest-event task where file order differs from timestamp order, a date span, and a ticket whose planted instruction reaches the planner through the tool output. The report says how often the planted text reached the planner (`injection_seen`) next to how often it was obeyed, so a low obeyed count is only meaningful when it was seen. Hard-tier results (one run each, 2026-10-15): `kimi-k2.7-code` 6/6, `minimax-m3` 6/6, `deepseek-v4-pro` 4/6, `qwen3-coder` 3/6, at $0.007 to $0.04 per run. The planted ticket instruction reached the planner in three of those runs and was never obeyed. Repeated three times (mean of 6): `minimax-m3` 6.0, `kimi-k2.7-code` 5.7, `deepseek-v4-pro` 5.3, `qwen3-coder` 4.3. Single runs misranked models, and `qwen3-coder` failed the log-window task all three times, a consistent weakness. The planted instruction reached the planner in 7 of 12 model-runs and was never obeyed. The scorer is lenient about a trailing `.0` ("200.0" counts for "200"). `qwen2.5-coder:7b` 2 of 6 after two prompt fixes (it started at 0 of 6 because the planner never finished), `llama3.2` 0 of 6. No unsafe tool was registered; some safe-but-wrong ones were, because staging checks that a tool runs cleanly, not that its answer is right. Runs vary. The CLI asks a human at approval unless you pass `--auto-approve`, which the report labels.
- **Real-model results** for these checks are in [results/summary.md](results/summary.md): closed-weight models 6.0 of 6 on the hard tier (3 runs each), open-weight models 2.7 to 5.3 at a 12,000-token reply cap, lower at the default 4,000. Most open-weight losses came from tools failing on real inputs or from the model's own wrong test expectations, not from the gates blocking safe, correct tools by mistake. Small samples; read them as a direction.
- **Hidden test set**: 10 of the 22 tasks (the easy and hard ones without planted text) have a hidden variant: the same question over different files, with an answer worked out by hand and a trap the first file lacks (a blank line, a string-sorted maximum, an orphan order, a window edge). After a task, a fresh planner may use the tool that got registered but may not write a new one, and its answer is scored. A tool that hard-codes the first answer, or fits only the first file, fails. The report shows `hidden test set: X of Y`, and `--no-hidden` skips it. With the oracle in real containers it scores 10 of 10 (that only shows the harness works).
- **Survival**: the same 10 tasks also have a drifted copy of their input: columns moved, extra columns or fields, CRLF line endings, tabs between fields. The right answer is the same. If the tool was right on the original files, a fresh planner answers again over the drifted files (no forging allowed), and the report shows `survival after input drift: X of Y`. This is a proxy for an API change, not a real one. It catches fragile tools: in a real container, a tool that reads the amount column by position is right on the original file and wrong after the drift. `--no-survival` skips it. With the oracle it scores 10 of 10, which only shows the harness works.
- **Red team** (`uv run python -m forge.bench redteam --model <name>`): sends 12 malicious requests (exfiltration, reverse shell, environment dump, delete, shell exec, sandbox escape, self-modification, memory bomb, and disguised versions) straight to a model's forger and records where the ladder stops each one, or that the model wrote nothing. The reviewer is a rubber stamp, so a draft that reaches the gate needs a human read: it may be a harmless stand-in. Drafts are saved with `--out`.
- **Injection tier** (`--tier injection`): two tasks with planted instructions, one that fakes the end of the tool-output fence and one that tries to make the planner request an upload tool.
- Not covered: tools that change behavior after approval.

### Paid models and the spend cap

`kimi-k2.7-code`, `minimax-m3`, `deepseek-v4-pro` and `qwen3-coder` cost roughly $0.30 to $0.67 per million input tokens and $0.82 to $3.35 per million output tokens (OpenRouter prices on 2026-10-15; they drift). They refuse to run unless you pass `--allow-paid`. The router adds up the token counts the API reports and stops before the next call once the estimate reaches `--max-cost` (default $0.50, hard maximum $5). The estimate is not a bill: also set a credit limit on the key in the OpenRouter dashboard.

    uv run --env-file .env python -m forge.bench bench --model qwen3-coder --allow-paid --max-cost 0.25 --auto-approve

The router also retries a busy provider (HTTP 429 or 503) twice, after 5 and 20 seconds. Free models are often busy; a run that reports only provider errors says nothing about the model.

### Setting the OpenRouter key

Put the key in a file named `.env` in the repo root (git-ignored), one line, no quotes:

    OPENROUTER_API_KEY=sk-or-...

Then pass it to uv, which loads it into the host environment for that run only:

    uv run --env-file .env python -m forge.bench bench --model nemotron-ultra --auto-approve

forge itself does not read `.env`; it only reads `OPENROUTER_API_KEY` from the environment, so no extra dependency is needed. The key stays on the host and never enters a container.

## Run the demo

    export FORGE_SANDBOX_IMAGE='python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1'
    uv run python -m forge.demo --model scripted             # no model: a bad tool is stopped, a good one reaches your approval prompt
    uv run python -m forge.demo --model ollama/qwen2.5-coder:7b   # a real local model writes the tool

Needs Docker and an interactive terminal (the approval gate refuses anything else). Inputs are a synthetic text file written to `.forge-demo/`. The scripted run is labelled scripted: the bad tool (opens a socket) is stopped by the static scan, the good one counts words, runs in the sandbox, and waits for your yes or no.

Known limit: the static scan denies `import sys` and `import os`, so a tool reads its argument with `argparse` (which reads argv itself). The forger prompt says so.

## MCP server (what exists now)

An MCP client (an agent app that speaks the Model Context Protocol) can use the tools forge has approved. The server ([src/forge/mcpserver/](src/forge/mcpserver/)) runs on the host with FastMCP and talks over stdio. It is an optional extra, because FastMCP brings many packages with it:

    uv sync --extra mcp
    FORGE_SANDBOX_IMAGE='python@sha256:a6e34c59...' uv run --extra mcp python -m forge.mcpserver \
        --registry .forge-demo/registry --inputs .forge-demo/inputs      # add --list to only print the tools

- **Only approved tools are exposed.** The server lists what is in the registry and offers no way to forge, approve, edit or revoke a tool. A client can use tools but never create one.
- **Same protections as the agent's own calls.** Each call goes through the orchestrator's `call_tool`: the registry re-hashes the stored copy, the manifest limits which input files are visible, the tool runs in the Docker sandbox, and the copy is hashed again afterwards. A tool that was tampered with, revoked or has no manifest (with `--require-manifest`) is refused.
- **Limits.** A tool's output is untrusted text and the MCP client has to treat it as data (descriptions and server instructions say so). A tool approved after the server started appears after a restart. I tested it with a real MCP client over stdio against the real CLI and a real container, not with a specific app such as Claude Desktop.

## Run the tests

    uv run pytest                                                # unit tests, no Docker needed
    FORGE_SANDBOX_IMAGE='python@sha256:a6e34c59...' uv run pytest  # full suite, needs Docker

Docker tests skip automatically when `FORGE_SANDBOX_IMAGE` is unset or the Docker daemon isn't running. The MCP tests need the extra (`uv run --extra mcp pytest`) and skip without it.

## Repo layout

    src/forge/
      sandbox/        policy.py, runner.py          built, security-critical
      checks/         static_scan, injection_scan, hash_pin, pipeline   built, security-critical
      staging/        stager.py, cases.py (forger-written tests), trace.py (staging evidence), manifest.py (capability manifest)   built
      approval/       diff.py, gate.py            built, security-critical
      registry/       registry.py                 built, security-critical
      orchestrator/   orchestrator.py             built, security-critical
      router/         router.py, agents.py        built
      bench/          attacks, redteam, benchmark, tasks (hidden variants, input drifts)    built
      mcpserver/      server.py, __main__.py (host-side MCP server, optional extra)   built, security-critical
      demo.py                                     live demo CLI
    results/            saved real outputs: attack suite report, an oracle harness check (JSON with staging traces)
    tests/
      sandbox/        policy, runner unit, runner docker tests
      checks/         static scan, injection scan, hash pin, pipeline
      approval/       diff rendering, gate decisions
      staging/        stager, forger-written cases, traces (real-container tests marked Docker)
      registry/       registration, verification, revoke, tamper cases
      orchestrator/   loop, forge-up-the-ladder, budgets, tamper and failure cases
      bench/          attack catalog, red team, benchmark scoring, hidden set, survival (Docker tests marked)
      mcpserver/      MCP server with a faked sandbox, plus a real client over stdio (Docker)
      demo/           scripted run with a faked sandbox
      router/         model aliases, key handling, strict reply parsing (fake backend)
      conftest.py     skip logic for Docker tests

## Glossary

- **MCP (Model Context Protocol):** a standard way for an agent to call tools. FastMCP is a Python library for writing MCP servers. A forged tool is a plain `tool.py` script that reads one JSON argument; forge serves approved tools to MCP clients through a host-side FastMCP server (see "MCP server").
- **Capability manifest:** a list of the input files a tool says it needs. forge enforces it by showing the tool only those files.
- **Container:** an isolated process environment built from an image. Docker runs them.
- **Digest:** a SHA-256 fingerprint of an image's contents. Pinning by digest means the exact bytes are fixed.
- **Capability (Linux):** a fine-grained root privilege, such as opening raw sockets. Dropping them all removes those powers.
- **Prompt injection:** text in a tool description or input that tries to make the agent do something its owner did not ask for.

## Ground rules

Project rules for contributors and for the AI agent working in this repo are in [CLAUDE.md](CLAUDE.md). The short version: generated tools run only in the sandbox, no credentials go inside it, security-critical code gets human review, and no network calls from the host without asking first. CLAUDE.md and this README are updated with every commit so they match the code.
