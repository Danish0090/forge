"""CLI.

    FORGE_SANDBOX_IMAGE='python@sha256:...' uv run python -m forge.bench attacks
    FORGE_SANDBOX_IMAGE='python@sha256:...' uv run python -m forge.bench bench --model oracle --auto-approve
    FORGE_SANDBOX_IMAGE='python@sha256:...' uv run python -m forge.bench bench --model ollama/qwen2.5-coder:7b

`bench` asks a real human at the approval step unless `--auto-approve` is passed. `oracle` is a
scripted upper bound, not a model. Any other --model goes through the router (host network calls).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from forge.approval.gate import ask_human
from forge.bench import attacks, benchmark
from forge.bench.tasks import TASKS
from forge.router import RouterError


def docker_ready() -> bool:
    """A benchmark with the daemon down would report every staging run as a model failure."""
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, check=False, timeout=30)
    return probe.returncode == 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge.bench")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("attacks", help="run the attack suite (needs Docker)")
    b = sub.add_parser("bench", help="run the benchmark for one model")
    b.add_argument("--model", required=True, help="oracle, ollama/<tag>, nemotron, gemma")
    b.add_argument("--tasks", help="comma-separated task ids (default: all)")
    b.add_argument("--auto-approve", action="store_true", help="answer yes at the human gate; recorded in the report")
    b.add_argument("--tier", help="easy or hard (default: all tasks)")
    b.add_argument("--allow-paid", action="store_true", help="permit paid models (spend is capped)")
    b.add_argument("--max-cost", type=float, help="spend cap in USD for the whole run (default 0.50, max 5)")
    b.add_argument("--out", type=Path, help="write the JSON report here")
    args = parser.parse_args(argv)

    image = os.environ.get("FORGE_SANDBOX_IMAGE", "")
    if not image:
        print("set FORGE_SANDBOX_IMAGE to the digest-pinned image (see README)", file=sys.stderr)
        return 2

    if not docker_ready():
        print("Docker is not running; start it first (staging runs would all fail and mean nothing)", file=sys.stderr)
        return 2

    if args.cmd == "attacks":
        results = attacks.run_suite(image)
        print(attacks.format_report(results))
        return 0 if not attacks.summarize(results)["unexpected"] else 1

    if args.tier and args.tier not in {t.tier for t in TASKS}:
        print("unknown tier; choose easy or hard", file=sys.stderr)
        return 2
    pool = [t for t in TASKS if not args.tier or t.tier == args.tier]
    ids = set(args.tasks.split(",")) if args.tasks else {t.id for t in pool}
    chosen = [t for t in pool if t.id in ids]
    if not chosen or len(chosen) != len(ids):
        print(f"unknown task id; choose from {', '.join(t.id for t in TASKS)}", file=sys.stderr)
        return 2
    try:
        factory = (
            benchmark.oracle_factory
            if args.model == "oracle"
            else benchmark.model_factory(args.model, allow_paid=args.allow_paid, max_cost_usd=args.max_cost)
        )
    except RouterError as exc:
        print(f"model error: {exc}", file=sys.stderr)
        return 2
    ask = (lambda _p: "yes") if args.auto_approve else ask_human
    show = (lambda _t: None) if args.auto_approve else print
    try:
        results = benchmark.run_benchmark(factory, image=image, tasks=chosen, ask=ask, show=show)
    except RouterError as exc:
        print(f"model error: {exc}", file=sys.stderr)
        return 2
    print(benchmark.format_report(args.model, results, auto_approve=args.auto_approve))
    if isinstance(factory, benchmark.ModelAgents) and factory.router.spent_usd:
        print(f"estimated spend: ${factory.router.spent_usd:.4f}")
    if args.out:
        args.out.write_text(json.dumps(benchmark.to_json(args.model, results, auto_approve=args.auto_approve), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
