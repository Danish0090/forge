"""Shared helpers for the benchmark tests: answers worked out without the reference tools, and a fake orchestrator."""

from __future__ import annotations

import csv
import datetime
import io
import json
from decimal import Decimal
from pathlib import Path

from forge.orchestrator.orchestrator import RunResult


def rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def worked_out(task_id: str, files: dict[str, str]) -> str:
    """The right answer, computed in the test without the reference tools."""
    if task_id == "word-count":
        return str(len(files["notes.txt"].split()))
    if task_id == "log-errors":
        return str(sum(" ERROR " in line for line in files["app.log"].splitlines()))
    if task_id == "csv-sum":
        return str(sum(int(r["amount"]) for r in rows(files["sales.csv"])))
    if task_id == "unique-users":
        return str(len({json.loads(line)["user"] for line in files["events.jsonl"].splitlines()}))
    if task_id == "top-score":
        return str(max(int(r["score"]) for r in rows(files["scores.csv"])))
    if task_id == "join-region":
        west = {r["customer_id"] for r in rows(files["customers.csv"]) if r["region"] == "west"}
        return str(sum(int(r["amount"]) for r in rows(files["orders.csv"]) if r["customer_id"] in west))
    if task_id == "messy-ledger":
        total = Decimal(0)
        for r in rows(files["ledger.csv"]):
            raw = r["amount"].replace("$", "").replace(",", "")
            total += Decimal(raw)
        return f"{total:.2f}"
    if task_id == "log-window":
        count = 0
        for line in files["server.log"].splitlines():
            time, level, *_ = line.split(None, 2)
            count += level == "ERROR" and "08:00:00" <= time < "08:30:00"
        return str(count)
    if task_id == "latest-action":
        latest: dict[str, dict] = {}
        for line in files["events2.jsonl"].splitlines():
            e = json.loads(line)
            if e["user"] not in latest or e["ts"] > latest[e["user"]]["ts"]:
                latest[e["user"]] = e
        return str(sum(e["action"] == "logout" for e in latest.values()))
    if task_id == "date-span":
        days = [datetime.date.fromisoformat(r["date"]) for r in rows(files["dates.csv"])]
        return str((max(days) - min(days)).days)
    raise AssertionError(f"no independent calculation for {task_id}")


def make_fake_orchestrator() -> type:
    """A fresh fake per test, so nothing set on it leaks into another test."""

    class Fake:
        built: list[dict] = []
        answer_for = staticmethod(lambda files, prompt: "")
        trace_items: list = []  # what every fake orchestrator reports as its staging traces

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.traces = list(type(self).trace_items)
            type(self).built.append(kwargs)

        def run(self, prompt: str) -> RunResult:
            inputs = Path(self.kwargs["inputs_dir"])  # type: ignore[arg-type]
            files = {p.name: p.read_bytes().decode("utf-8") for p in inputs.iterdir()}  # exact bytes, so CRLF survives
            answer = type(self).answer_for(files, prompt)
            first = len(type(self).built) == 1  # the main run is built before any hidden run
            return RunResult("done", answer, ("word_count",) if first else (), ())

    return Fake
