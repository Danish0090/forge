"""Serve approved tools over MCP (stdio).

    FORGE_SANDBOX_IMAGE='python@sha256:...' uv run --extra mcp python -m forge.mcpserver \\
        --registry .forge-demo/registry --inputs .forge-demo/inputs

`--list` prints the tools that would be served and exits. While serving, stdout carries the protocol, so
messages go to stderr. The registry folder must already exist: serving never creates one.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from forge.bench.__main__ import docker_ready
from forge.mcpserver import ForgeMcp
from forge.registry import Registry


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge.mcpserver", description="Serve approved forge tools over MCP (stdio).")
    parser.add_argument("--registry", type=Path, default=Path(".forge-demo/registry"), help="registry folder (must exist)")
    parser.add_argument("--inputs", type=Path, default=Path(".forge-demo/inputs"), help="read-only inputs folder (must exist)")
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds a tool may run")
    parser.add_argument("--require-manifest", action="store_true", help="refuse tools that have no manifest")
    parser.add_argument("--list", action="store_true", help="print the tools that would be served and exit")
    args = parser.parse_args(argv)

    image = os.environ.get("FORGE_SANDBOX_IMAGE", "")
    if not image:
        print("set FORGE_SANDBOX_IMAGE to the digest-pinned image (see README)", file=sys.stderr)
        return 2
    if not docker_ready():
        print("Docker is not running; start it first", file=sys.stderr)
        return 2
    if not args.registry.is_dir():
        print(f"registry folder not found: {args.registry}", file=sys.stderr)
        return 2
    if not args.inputs.is_dir():
        print(f"inputs folder not found: {args.inputs}", file=sys.stderr)
        return 2

    forge = ForgeMcp(
        Registry(args.registry),
        image=image,
        inputs_dir=args.inputs.resolve(),
        timeout_s=args.timeout,
        require_manifest=args.require_manifest,
    )
    tools = sorted(forge._listed)  # noqa: SLF001 - the names are all this needs
    if args.list:
        print("\n".join(tools) if tools else "(no approved tools)")
        return 0
    print(f"serving {len(tools)} approved tool(s) over MCP: {', '.join(tools) or 'none'}", file=sys.stderr)
    forge.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
