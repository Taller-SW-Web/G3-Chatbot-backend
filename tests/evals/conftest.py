"""H5-E1 eval plugin: one `evals-report.json` with case, status, tools and assertions.

The report is written even when cases fail or skip. It is uploaded as a CI
artifact by `.github/workflows/evals.yml`; locally it is ignored by git.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.evals.harness import EvalCase

_CASES: dict[str, EvalCase] = {}


def _report_path(session: pytest.Session) -> Path:
    configured = Path(os.environ.get("EVALS_REPORT_PATH", "evals-report.json"))
    return configured if configured.is_absolute() else Path(session.config.rootpath) / configured


def pytest_sessionstart(session: pytest.Session) -> None:
    _CASES.clear()
    _report_path(session).unlink(missing_ok=True)


@pytest.fixture
def eval_case(request: pytest.FixtureRequest) -> EvalCase:
    """Register one reference conversation; the docstring documents the scenario."""
    scenario = (request.function.__doc__ or request.node.name).strip()
    case = EvalCase(name=request.node.name, scenario=scenario)
    _CASES[request.node.nodeid] = case
    return case


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    case = _CASES.get(report.nodeid)
    if case is None or report.when not in ("setup", "call"):
        return
    if report.failed:
        case.status = "failed"
        case.failure = str(report.longrepr)
    elif report.skipped:
        case.status = "skipped"
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        case.skip_reason = str(reason)
    elif report.when == "call" and report.passed:
        case.status = "passed"
    if report.when == "call":
        case.duration_ms = int(report.duration * 1000)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _CASES:
        return
    cases = list(_CASES.values())
    summary: dict[str, Any] = {
        "total": len(cases),
        "passed": sum(case.status == "passed" for case in cases),
        "failed": sum(case.status == "failed" for case in cases),
        "skipped": sum(case.status == "skipped" for case in cases),
    }
    payload = {
        "suite": "H5-E1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
        "summary": summary,
        "cases": [case.to_dict() for case in cases],
    }
    path = _report_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
