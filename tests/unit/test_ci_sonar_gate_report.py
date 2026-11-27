from __future__ import annotations

from scripts.ci.sonar_gate_report import format_report

GATE = {
    "status": "ERROR",
    "conditions": [
        {
            "metricKey": "new_coverage",
            "status": "ERROR",
            "actualValue": "72.4",
            "comparator": "LT",
            "errorThreshold": "80",
        },
        {
            "metricKey": "new_violations",
            "status": "OK",
            "actualValue": "0",
            "comparator": "GT",
            "errorThreshold": "0",
        },
    ],
}

ISSUES = [
    {
        "component": "g3-chatbot-backend:src/modules/mod_catalog/application/search.py",
        "line": 42,
        "severity": "MAJOR",
        "rule": "python:S3776",
        "message": "Reduce Cognitive Complexity | split the function",
    }
]


def test_format_report_includes_conditions_and_issues() -> None:
    report = format_report(
        project_key="g3-chatbot-backend",
        gate=GATE,
        issues=ISSUES,
        pull_request="123",
    )
    assert "PR #123" in report
    assert "**ERROR**" in report
    assert "new_coverage" in report
    assert "src/modules/mod_catalog/application/search.py" in report
    assert "python:S3776" in report
    assert "\\|" in report


def test_format_report_without_issues() -> None:
    report = format_report(
        project_key="g3-chatbot-backend",
        gate={"status": "OK", "conditions": []},
        issues=[],
        branch="development",
    )
    assert "rama `development`" in report
    assert "Sin issues abiertos" in report
