"""Attack suite tests. Checks-stage attacks need no Docker; the full catalog runs in containers."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.bench.attacks import CATALOG, Attack, AttackResult, format_report, run_attack, run_suite, summarize

DIGEST = "python@sha256:" + "a" * 64


def test_catalog_ids_are_unique_and_cover_every_stage() -> None:
    ids = [a.id for a in CATALOG]
    assert len(ids) == len(set(ids))
    assert {a.expected for a in CATALOG} == {"checks", "staging", "approval"}
    assert any(not a.malicious for a in CATALOG), "controls are needed to catch over-blocking"


@pytest.mark.parametrize("attack", [a for a in CATALOG if a.expected == "checks"], ids=lambda a: a.id)
def test_scan_attacks_stop_at_checks_without_docker(attack: Attack, tmp_path: Path) -> None:
    # The fake digest is never used: a draft that fails the checks never reaches the sandbox.
    result = run_attack(attack, DIGEST, tmp_path)
    assert result.actual == "checks", result.detail


def test_summary_counts_and_flags_unexpected() -> None:
    a = Attack("a", "", "", "checks")
    b = Attack("b", "", "", "staging")
    ctl = Attack("c", "", "", "approval", malicious=False)
    results = [AttackResult(a, "checks", ""), AttackResult(b, "approval", ""), AttackResult(ctl, "checks", "")]
    s = summarize(results)
    assert s["attacks"] == 2 and s["stopped_automatically"] == 1
    assert s["reached_human"] == ["b"] and s["controls_blocked"] == ["c"]
    assert s["unexpected"] == ["b", "c"]
    assert "1 of 2 attacks stopped" in format_report(results)


@pytest.mark.docker
def test_full_catalog_matches_expected_outcomes(sandbox_image: str) -> None:
    results = run_suite(sandbox_image)
    wrong = [(r.attack.id, r.attack.expected, r.actual, r.detail) for r in results if not r.as_expected]
    assert not wrong, wrong
    print("\n" + format_report(results))
