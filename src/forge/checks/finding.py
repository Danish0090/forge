"""The one shape every check reports in, so the pipeline can print them all the same way."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    check: str  # which rule fired, e.g. "denied-import"
    message: str  # what a human should read
    path: str = ""  # file, relative to the tool folder; empty for text checks
    line: int | None = None
