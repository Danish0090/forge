"""Stage 2 of the trust ladder: run every check and report all findings together.

The pipeline never stops at the first failure. A reviewer should see every problem at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from forge.checks.finding import Finding
from forge.checks.hash_pin import verify_pin
from forge.checks.injection_scan import scan_text
from forge.checks.static_scan import scan_tool_dir


@dataclass(frozen=True)
class CheckReport:
    findings: tuple[Finding, ...]

    @property
    def passed(self) -> bool:
        return not self.findings


def run_checks(tool_dir: Path, description: str, pinned_sha256: str) -> CheckReport:
    """Run static scan, injection scan, and hash pin. A tool passes only with zero findings."""
    findings: list[Finding] = []
    findings.extend(scan_tool_dir(tool_dir))
    findings.extend(scan_text(description))
    findings.extend(verify_pin(tool_dir, pinned_sha256))
    return CheckReport(findings=tuple(findings))


__all__ = ["CheckReport", "Finding", "run_checks"]
