from __future__ import annotations

from scripts.ci.sonar_gate_report import (
    _measure_value,
    blocking_issues,
    format_report,
    measure_failures,
)

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


def test_measure_value_reads_periods_and_falls_back_to_value() -> None:
    assert (
        _measure_value(
            {"metric": "new_coverage", "periods": [{"index": 1, "value": "100.0"}]}
        )
        == "100.0"
    )
    assert _measure_value({"metric": "coverage", "value": "85.1"}) == "85.1"
    assert _measure_value({"metric": "new_lines", "periods": []}) == ""


def test_measure_failures_fail_closed_on_missing_new_code_measures() -> None:
    assert measure_failures(
        {"new_lines": "52", "new_lines_to_cover": "9", "new_coverage": "100.0", "new_duplicated_lines_density": "0.0"},
        min_new_coverage=80,
        max_new_duplication=3,
    ) == []
    assert measure_failures(
        {"new_lines": "52", "new_lines_to_cover": "9", "new_duplicated_lines_density": "0.0"},
        min_new_coverage=80,
        max_new_duplication=3,
    ) == ["new code tiene líneas que cubrir pero Sonar no publicó new_coverage"]
    assert measure_failures(
        {"new_lines": "52", "new_lines_to_cover": "9", "new_coverage": "75.0", "new_duplicated_lines_density": "1.0"},
        min_new_coverage=80,
        max_new_duplication=3,
    ) == ["cobertura de new code 75.0% < 80%"]
    assert measure_failures(
        {"new_lines": "52", "new_lines_to_cover": "9", "new_coverage": "90.0", "new_duplicated_lines_density": "4.5"},
        min_new_coverage=80,
        max_new_duplication=3,
    ) == ["duplicación de new code 4.5% > 3%"]
    assert measure_failures(
        {"new_lines": "10"},
        min_new_coverage=80,
        max_new_duplication=3,
    ) == [
        "new code tiene líneas pero Sonar no publicó new_duplicated_lines_density"
    ]
    assert measure_failures(
        {},
        min_new_coverage=80,
        max_new_duplication=3,
        require_metrics=True,
    ) == ["el PR toca src/ pero Sonar no publicó new_lines/new_lines_to_cover"]
    assert measure_failures(
        {},
        min_new_coverage=80,
        max_new_duplication=3,
        require_metrics=False,
    ) == []


def test_blocking_issues_respects_the_severity_threshold() -> None:
    issues = [
        {"severity": "INFO", "rule": "python:S1"},
        {"severity": "MINOR", "rule": "python:S2"},
        {"severity": "MAJOR", "rule": "python:S3"},
        {"severity": "BLOCKER", "rule": "python:S4"},
    ]
    assert [issue["severity"] for issue in blocking_issues(issues, "MINOR")] == [
        "MINOR",
        "MAJOR",
        "BLOCKER",
    ]
    assert [issue["severity"] for issue in blocking_issues(issues, "MAJOR")] == [
        "MAJOR",
        "BLOCKER",
    ]
    assert blocking_issues(issues, "BLOCKER") == [issues[3]]
    assert blocking_issues([], "INFO") == []
