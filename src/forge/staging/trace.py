"""Staging traces: what one sandbox run did, kept as evidence.

A trace holds the command, exit code, wall time and capped output of a run. It is shown to the
reviewer and saved in benchmark reports so a failure or a surprise can be looked at afterwards.
It never decides anything: approval and the registry do not read traces.
Output is capped here because a tool, not the host, produced it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from forge.sandbox.runner import SandboxResult

MAX_OUTPUT_CHARS = 300
MAX_ARGV_CHARS = 200
_ARG_CLIP = 100


@dataclass(frozen=True)
class StagingTrace:
    label: str  # "main" for the run on the real inputs, "case N" for a forger-written test
    argv: tuple[str, ...]
    exit_code: int | None  # None when the run was killed by the timeout
    seconds: float
    stdout: str
    stderr: str
    stdout_bytes: int  # real size of the output, kept even though the text is cut
    timed_out: bool
    tool: str = ""  # filled in by the orchestrator


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def make_trace(label: str, argv: Sequence[str], result: SandboxResult, seconds: float) -> StagingTrace:
    return StagingTrace(
        label=label,
        argv=tuple(_clip(a, _ARG_CLIP) for a in argv),
        exit_code=result.exit_code,
        seconds=round(seconds, 3),
        stdout=_clip(result.stdout, MAX_OUTPUT_CHARS),
        stderr=_clip(result.stderr, MAX_OUTPUT_CHARS),
        stdout_bytes=len(result.stdout.encode("utf-8", errors="replace")),
        timed_out=result.timed_out,
    )


def summarize_traces(traces: Sequence[StagingTrace]) -> str:
    """One short line for the reviewer. Empty when there is nothing to say."""
    parts = []
    main = next((t for t in traces if t.label == "main"), None)
    if main is not None:
        how = "timed out" if main.timed_out else f"exit {main.exit_code}"
        parts.append(f"main run {how} in {main.seconds}s, {main.stdout_bytes} bytes of output")
    cases = [t for t in traces if t.label.startswith("case")]
    if cases:
        bad = sum(t.timed_out or t.exit_code != 0 for t in cases)
        total = round(sum(t.seconds for t in cases), 3)
        tail = f", {bad} failed to exit cleanly" if bad else ""
        parts.append(f"{len(cases)} test case runs in {total}s{tail}")
    return "Staging trace: " + "; ".join(parts) if parts else ""
