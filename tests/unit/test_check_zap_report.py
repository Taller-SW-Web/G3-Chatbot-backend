"""Unit tests for the OWASP ZAP DAST report gate (H5-C2)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_zap_report import evaluate_reports, load_ignored_plugin_ids


def _write_report(path: Path, alerts: list[dict[str, object]]) -> Path:
    payload = {
        "site": [
            {
                "@name": "http://127.0.0.1:8000",
                "alerts": alerts,
            }
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_passes_with_no_alerts(tmp_path: Path) -> None:
    report = _write_report(tmp_path / "zap-api.json", [])
    summary = tmp_path / "summary.md"

    code = evaluate_reports([report], summary_output=summary)

    assert code == 0
    content = summary.read_text(encoding="utf-8")
    assert "Sin alertas reportadas" in content
    assert "**Resultado: OK**" in content


def test_passes_with_medium_low_and_informational_alerts(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path / "zap-api.json",
        [
            {"pluginid": "10021", "name": "X-Content-Type-Options Missing", "riskcode": "1", "count": "4"},
            {"pluginid": "10038", "name": "CSP Header Not Set", "riskcode": "2", "count": "2"},
            {"pluginid": "10027", "name": "Information Disclosure", "riskcode": "0", "count": "1"},
        ],
    )
    summary = tmp_path / "summary.md"

    code = evaluate_reports([report], summary_output=summary)

    assert code == 0
    content = summary.read_text(encoding="utf-8")
    assert "| High | 0 |" in content
    assert "| Medium | 1 |" in content
    assert "| Low | 1 |" in content
    assert "| Informational | 1 |" in content


def test_blocks_on_unignored_high_alert(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path / "zap-api.json",
        [
            {"pluginid": "40018", "name": "SQL Injection", "riskcode": "3", "count": "1"},
            {"pluginid": "10021", "name": "X-Content-Type-Options Missing", "riskcode": "1", "count": "2"},
        ],
    )
    summary = tmp_path / "summary.md"

    code = evaluate_reports([report], summary_output=summary)

    assert code == 1
    content = summary.read_text(encoding="utf-8")
    assert "**Resultado: FALLO**" in content
    assert "`40018`" in content
    assert "Bloqueante" in content


def test_allows_high_alert_when_explicitly_ignored_in_rules(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path / "zap-api.json",
        [
            {"pluginid": "40018", "name": "Example Justified Alert", "riskcode": "3", "count": "1"},
        ],
    )
    rules = tmp_path / "rules.tsv"
    rules.write_text("# comment\n40018\tIGNORE\tDocumented in zap-exceptions.md\n", encoding="utf-8")
    summary = tmp_path / "summary.md"

    assert load_ignored_plugin_ids(rules) == {"40018"}
    code = evaluate_reports([report], rules_path=rules, summary_output=summary)

    assert code == 0
    content = summary.read_text(encoding="utf-8")
    assert "Exceptuada (`rules.tsv`)" in content
    assert "**Resultado: OK**" in content


def test_aggregates_multiple_reports_and_fails_on_missing_file(tmp_path: Path) -> None:
    api_report = _write_report(
        tmp_path / "zap-api.json",
        [{"pluginid": "10021", "name": "Header Missing", "riskcode": "1", "count": "2"}],
    )
    health_report = _write_report(
        tmp_path / "zap-health.json",
        [{"pluginid": "10021", "name": "Header Missing", "riskcode": "1", "count": "1"}],
    )
    summary = tmp_path / "summary.md"

    code = evaluate_reports([api_report, health_report], summary_output=summary)

    assert code == 0
    assert "| `10021` | Low | Header Missing | 3 |" in summary.read_text(encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        evaluate_reports([tmp_path / "missing.json"])
