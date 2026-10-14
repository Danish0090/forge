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
import sys
from collections.abc import Sequence
from pathlib import Path

from forge.approval.gate import ask_human
from forge.bench import attacks, benchmark
from forge.bench.tasks import TASKS
from forge.router import RouterError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge.bench")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("attacks", help="run the attack suite (needs Docker)")
    b = sub.add_parser("bench", help="run the benchmark for one model")
    b.add_argument("--model", required=True, help="oracle, ollama/<tag>, nemotron, gemma")
    b.add_argument("--tasks", help="comma-separated task ids (default: all)")
    b.add_argument("--auto-approve", action="store_true", help="answer yes at the human gate; recorded in the report")
    b.add_argument("--out", type=Path, help="write the JSON report here")
    args = parser.parse_args(argv)

    image = os.environ.get("FORGE_SANDBOX_IMAGE", "")
    if not image:
        print("set FORGE_SANDBOX_IMAGE to the digest-pinned image (see README)", file=sys.stderr)
        return 2

    if args.cmd == "attacks":
        results = attacks.run_suite(image)
        print(attacks.format_report(results))
        return 0 if not attacks.summarize(results)["unexpected"] else 1

    ids = set(args.tasks.split(",")) if args.tasks else {t.id for t in TASKS}
    chosen = [t for t in TASKS if t.id in ids]
    if not chosen or len(chosen) != len(ids):
        print(f"unknown task id; choose from {', '.join(t.id for t in TASKS)}", file=sys.stderr)
        return 2
    factory = benchmark.oracle_factory if args.model == "oracle" else benchmark.model_factory(args.model)
    ask = (lambda _p: "yes") if args.auto_approve else ask_human
    show = (lambda _t: None) if args.auto_approve else print
    try:
        results = benchmark.run_benchmark(factory, image=image, tasks=chosen, ask=ask, show=show)
    except RouterError as exc:
        print(f"model error: {exc}", file=sys.stderr)
        return 2
    print(benchmark.format_report(args.model, results, auto_approve=args.auto_approve))
    if args.out:
        args.out.write_text(json.dumps(benchmark.to_json(args.model, results, auto_approve=args.auto_approve), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
