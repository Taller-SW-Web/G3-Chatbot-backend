"""Evaluates OWASP ZAP JSON reports and enforces the H5-C2 DAST gate.

Usage:
    python scripts/check_zap_report.py \
        --report zap-reports/zap-api-report.json \
        --report zap-reports/zap-health-report.json \
        --rules .zap/rules.tsv \
        --summary-output zap-reports/summary.md

Policy (H5-C2):
- High severity findings (`riskcode == "3"`) fail the job unless explicitly
  ignored by plugin ID in `--rules` (and documented in
  `docs/seguridad/zap-exceptions.md`).
- Medium, Low and Informational findings are reported in the summary without
  blocking the merge.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass

RISK_LABELS: dict[str, str] = {
    "3": "High",
    "2": "Medium",
    "1": "Low",
    "0": "Informational",
}
HIGH_RISK_CODE = "3"


@dataclass(frozen=True)
class AlertSummary:
    plugin_id: str
    name: str
    risk_code: str
    instances: int
    ignored: bool

    @property
    def risk_label(self) -> str:
        return RISK_LABELS.get(self.risk_code, f"Unknown({self.risk_code})")


def load_ignored_plugin_ids(rules_path: pathlib.Path | None) -> set[str]:
    """Reads plugin IDs marked as IGNORE in a ZAP TSV rules file."""
    if rules_path is None or not rules_path.is_file():
        return set()
    ignored: set[str] = set()
    for raw_line in rules_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].strip().upper() == "IGNORE":
            ignored.add(parts[0].strip())
    return ignored


def load_documented_plugin_ids(doc_path: pathlib.Path | None) -> set[str]:
    """Plugin IDs documented in the exceptions table of the security doc."""
    if doc_path is None:
        return set()
    if not doc_path.is_file():
        raise FileNotFoundError(f"ZAP exceptions doc not found: {doc_path}")
    text = doc_path.read_text(encoding="utf-8")
    return set(re.findall(r"`([0-9]+)`", text))


def parse_zap_reports(
    report_paths: list[pathlib.Path],
    ignored_ids: set[str],
) -> list[AlertSummary]:
    """Aggregates alerts across one or more ZAP JSON reports."""
    aggregated: dict[tuple[str, str, str], int] = {}
    for report_path in report_paths:
        if not report_path.is_file():
            raise FileNotFoundError(f"ZAP report not found: {report_path}")
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        sites = payload.get("site") or []
        if isinstance(sites, dict):
            sites = [sites]
        for site in sites:
            for alert in site.get("alerts") or []:
                plugin_id = str(alert.get("pluginid") or alert.get("alertRef") or "unknown")
                name = str(alert.get("name") or alert.get("alert") or "Unnamed alert")
                risk_code = str(alert.get("riskcode", "0"))
                raw_count = alert.get("count")
                if raw_count is not None:
                    count = int(raw_count)
                else:
                    count = len(alert.get("instances") or []) or 1
                key = (plugin_id, name, risk_code)
                aggregated[key] = aggregated.get(key, 0) + count

    summaries = [
        AlertSummary(
            plugin_id=plugin_id,
            name=name,
            risk_code=risk_code,
            instances=instances,
            ignored=plugin_id in ignored_ids,
        )
        for (plugin_id, name, risk_code), instances in aggregated.items()
    ]
    summaries.sort(key=lambda item: (-int(item.risk_code) if item.risk_code.isdigit() else 0, item.plugin_id))
    return summaries


def render_markdown_summary(
    alerts: list[AlertSummary],
    report_paths: list[pathlib.Path],
    undocumented_ids: list[str] | None = None,
) -> str:
    """Renders a GitHub Step Summary table for the ZAP DAST scan."""
    counts = {"High": 0, "Medium": 0, "Low": 0, "Informational": 0}
    blocking: list[AlertSummary] = []
    for alert in alerts:
        if alert.risk_label in counts:
            counts[alert.risk_label] += 1
        if alert.risk_code == HIGH_RISK_CODE and not alert.ignored:
            blocking.append(alert)

    lines = [
        "## DAST — OWASP ZAP Baseline (H5-C2)",
        "",
        f"Reportes analizados: {', '.join(f'`{p.name}`' for p in report_paths)}",
        "",
        "| Severidad | Alertas distintas |",
        "|---|---|",
        f"| High | {counts['High']} |",
        f"| Medium | {counts['Medium']} |",
        f"| Low | {counts['Low']} |",
        f"| Informational | {counts['Informational']} |",
        "",
    ]

    if alerts:
        lines.extend(
            [
                "| Plugin ID | Severidad | Alerta | Instancias | Estado |",
                "|---|---|---|---|---|",
            ]
        )
        for alert in alerts:
            if alert.ignored:
                status = "Exceptuada (`rules.tsv`)"
            elif alert.risk_code == HIGH_RISK_CODE:
                status = "Bloqueante"
            else:
                status = "Informativa"
            lines.append(
                f"| `{alert.plugin_id}` | {alert.risk_label} | {alert.name} | {alert.instances} | {status} |"
            )
    else:
        lines.append("Sin alertas reportadas por OWASP ZAP.")

    undocumented = undocumented_ids or []
    if undocumented:
        lines.append(
            f"**Excepciones sin documentar:** {', '.join(f'`{item}`' for item in undocumented)}"
        )

    lines.append("")
    if blocking or undocumented:
        reasons = []
        if blocking:
            reasons.append(f"{len(blocking)} alerta(s) High sin excepción justificada")
        if undocumented:
            reasons.append(f"{len(undocumented)} excepción(es) sin documentar")
        lines.append(f"**Resultado: FALLO** — {'; '.join(reasons)}.")
    else:
        lines.append("**Resultado: OK** — 0 alertas High sin excepción.")
    lines.append("")
    return "\n".join(lines)


def evaluate_reports(
    report_paths: list[pathlib.Path],
    rules_path: pathlib.Path | None = None,
    summary_output: pathlib.Path | None = None,
    exceptions_doc: pathlib.Path | None = None,
) -> int:
    ignored_ids = load_ignored_plugin_ids(rules_path)
    documented_ids = load_documented_plugin_ids(exceptions_doc)
    undocumented = sorted(ignored_ids - documented_ids) if exceptions_doc is not None else []
    alerts = parse_zap_reports(report_paths, ignored_ids)
    summary = render_markdown_summary(alerts, report_paths, undocumented)

    if summary_output is not None:
        summary_output.parent.mkdir(parents=True, exist_ok=True)
        summary_output.write_text(summary, encoding="utf-8")

    print(summary)
    blocking = [a for a in alerts if a.risk_code == HIGH_RISK_CODE and not a.ignored]
    return 1 if blocking or undocumented else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate OWASP ZAP JSON reports for CI.")
    parser.add_argument(
        "--report",
        action="append",
        required=True,
        help="Path to a ZAP JSON report (repeatable).",
    )
    parser.add_argument(
        "--rules",
        default=None,
        help="Optional path to ZAP rules.tsv containing justified IGNORE entries.",
    )
    parser.add_argument(
        "--summary-output",
        default=None,
        help="Optional path to write the Markdown summary.",
    )
    parser.add_argument(
        "--exceptions-doc",
        default=None,
        help="Markdown doc whose exception table must contain every IGNORE plugin ID.",
    )
    args = parser.parse_args(argv)

    report_paths = [pathlib.Path(p) for p in args.report]
    rules_path = pathlib.Path(args.rules) if args.rules else None
    summary_output = pathlib.Path(args.summary_output) if args.summary_output else None
    exceptions_doc = pathlib.Path(args.exceptions_doc) if args.exceptions_doc else None
    return evaluate_reports(
        report_paths,
        rules_path=rules_path,
        summary_output=summary_output,
        exceptions_doc=exceptions_doc,
    )


if __name__ == "__main__":
    sys.exit(main())
