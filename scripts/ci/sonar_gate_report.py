"""Explain a failed SonarCloud quality gate without opening the UI.

The scan action already fails the job when ``sonar.qualitygate.wait`` is true.
This script turns that failure into a readable table of conditions and issues
for the pull request or branch, and appends it to the job summary.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

DEFAULT_HOST = "https://sonarcloud.io"
MAX_ISSUES = 50
MAX_PAGES = 5
MEASURE_KEYS = (
    "new_lines,new_lines_to_cover,new_coverage,"
    "new_duplicated_lines_density,new_violations"
)
SEVERITY_ORDER = {
    "INFO": 0,
    "MINOR": 1,
    "MAJOR": 2,
    "CRITICAL": 3,
    "BLOCKER": 4,
}


def _api_get(
    host: str, path: str, params: Mapping[str, str], token: str
) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{host.rstrip('/')}{path}?{query}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_gate_status(
    *,
    host: str,
    token: str,
    project_key: str,
    pull_request: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    params = {"projectKey": project_key}
    if pull_request:
        params["pullRequest"] = pull_request
    elif branch:
        params["branch"] = branch
    return _api_get(host, "/api/qualitygates/project_status", params, token)


def fetch_issues(
    *,
    host: str,
    token: str,
    project_key: str,
    pull_request: str | None = None,
    branch: str | None = None,
    limit: int = MAX_ISSUES,
    fetch_all: bool = False,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        params: dict[str, str] = {
            "componentKeys": project_key,
            "resolved": "false",
            "p": str(page),
            "ps": "100",
            "s": "SEVERITY",
            "asc": "false",
        }
        if pull_request:
            params["pullRequest"] = pull_request
        elif branch:
            params["branch"] = branch
            params["inNewCodePeriod"] = "true"
        payload = _api_get(host, "/api/issues/search", params, token)
        batch = list(payload.get("issues", []))
        issues.extend(batch)
        total = int(payload.get("total", len(issues)))
        if not fetch_all and len(issues) >= limit:
            break
        if not batch or len(issues) >= total:
            break
    return issues if fetch_all else issues[:limit]


def fetch_measures(
    *,
    host: str,
    token: str,
    project_key: str,
    pull_request: str | None = None,
    branch: str | None = None,
) -> dict[str, str]:
    params: dict[str, str] = {
        "component": project_key,
        "metricKeys": MEASURE_KEYS,
    }
    if pull_request:
        params["pullRequest"] = pull_request
    elif branch:
        params["branch"] = branch
    payload = _api_get(host, "/api/measures/component", params, token)
    measures = payload.get("component", {}).get("measures", [])
    return {
        str(measure.get("metric")): _measure_value(measure)
        for measure in measures
    }


def _measure_value(measure: Mapping[str, Any]) -> str:
    """SonarCloud returns new-code metrics under ``periods``, not ``value``."""
    periods = measure.get("periods") or []
    if periods:
        return str(periods[-1].get("value", ""))
    return str(measure.get("value", ""))


def _as_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def measure_failures(
    measures: Mapping[str, str],
    *,
    min_new_coverage: float,
    max_new_duplication: float,
    require_metrics: bool = False,
) -> list[str]:
    """Return the new-code metric violations, failing closed when a measure is missing."""
    failures: list[str] = []
    new_lines = _as_float(measures.get("new_lines"))
    lines_to_cover = _as_float(measures.get("new_lines_to_cover"))
    new_coverage = _as_float(measures.get("new_coverage"))
    new_duplication = _as_float(measures.get("new_duplicated_lines_density"))

    if require_metrics and new_lines is None and lines_to_cover is None:
        failures.append(
            "el PR toca src/ pero Sonar no publicó new_lines/new_lines_to_cover"
        )
        return failures
    if lines_to_cover is not None and lines_to_cover > 0:
        if new_coverage is None:
            failures.append(
                "new code tiene líneas que cubrir pero Sonar no publicó new_coverage"
            )
        elif new_coverage < min_new_coverage:
            failures.append(
                f"cobertura de new code {new_coverage}% < {min_new_coverage}%"
            )
    if new_lines is not None and new_lines > 0:
        if new_duplication is None:
            failures.append(
                "new code tiene líneas pero Sonar no publicó new_duplicated_lines_density"
            )
        elif new_duplication > max_new_duplication:
            failures.append(
                f"duplicación de new code {new_duplication}% > {max_new_duplication}%"
            )
    return failures


def blocking_issues(
    issues: list[Mapping[str, Any]], threshold: str
) -> list[Mapping[str, Any]]:
    minimum = SEVERITY_ORDER[threshold.upper()]
    return [
        issue
        for issue in issues
        if SEVERITY_ORDER.get(str(issue.get("severity", "")).upper(), -1) >= minimum
    ]


def _escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def format_report(
    *,
    project_key: str,
    gate: Mapping[str, Any],
    issues: list[Mapping[str, Any]],
    pull_request: str | None = None,
    branch: str | None = None,
) -> str:
    status = gate.get("status", "UNKNOWN")
    conditions = gate.get("conditions", [])
    location = f"PR #{pull_request}" if pull_request else f"rama `{branch}`"
    lines = [
        "## SonarCloud quality gate",
        "",
        f"Proyecto `{project_key}` · {location} · estado: **{status}**",
        "",
        "| Condición | Estado | Valor | Umbral |",
        "|---|---|---|---|",
    ]
    for condition in conditions:
        lines.append(
            "| {metric} | {status} | {actual} | {threshold} |".format(
                metric=_escape(condition.get("metricKey", "")),
                status=_escape(condition.get("status", "")),
                actual=_escape(condition.get("actualValue", "—")),
                threshold=_escape(
                    f"{condition.get('comparator', '')} {condition.get('errorThreshold', '')}".strip()
                ),
            )
        )
    if not conditions:
        lines.append("| (sin condiciones reportadas) | — | — | — |")

    lines.extend(["", f"### Issues abiertos ({len(issues)})", ""])
    if issues:
        lines.extend(
            [
                "| Archivo | Línea | Severidad | Regla | Mensaje |",
                "|---|---|---|---|---|",
            ]
        )
        for issue in issues:
            component = str(issue.get("component", ""))
            file_path = component.split(":", 1)[-1]
            lines.append(
                "| {file} | {line} | {severity} | {rule} | {message} |".format(
                    file=_escape(file_path),
                    line=_escape(issue.get("line", "—")),
                    severity=_escape(issue.get("severity", "")),
                    rule=_escape(issue.get("rule", "")),
                    message=_escape(issue.get("message", ""))[:180],
                )
            )
    else:
        lines.append("Sin issues abiertos en el alcance analizado.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--host", default=os.getenv("SONAR_HOST_URL", DEFAULT_HOST))
    parser.add_argument("--pull-request")
    parser.add_argument("--branch")
    parser.add_argument("--output", type=Path, default=Path("sonar-gate-report.md"))
    parser.add_argument("--max-issues", type=int, default=MAX_ISSUES)
    parser.add_argument(
        "--fail-on-severity",
        choices=tuple(SEVERITY_ORDER),
        help="Fail when the PR/branch introduces an issue at or above this severity.",
    )
    parser.add_argument(
        "--min-new-coverage",
        type=float,
        default=80.0,
        help="Minimum coverage of new code when the strict gate is enabled.",
    )
    parser.add_argument(
        "--max-new-duplication",
        type=float,
        default=3.0,
        help="Maximum duplicated-lines density of new code.",
    )
    parser.add_argument(
        "--require-new-code-metrics",
        action="store_true",
        help="Fail closed when a PR that changes src/ has no new-code measures.",
    )
    args = parser.parse_args(argv)

    token = os.getenv("SONAR_TOKEN")
    if not token:
        if args.fail_on_severity:
            print("::error::SONAR_TOKEN is required for the strict gate.")
            return 1
        print("::warning::SONAR_TOKEN is not available; skipping the gate report.")
        return 0

    try:
        gate = fetch_gate_status(
            host=args.host,
            token=token,
            project_key=args.project_key,
            pull_request=args.pull_request,
            branch=args.branch,
        ).get("projectStatus", {})
        issues = fetch_issues(
            host=args.host,
            token=token,
            project_key=args.project_key,
            pull_request=args.pull_request,
            branch=args.branch,
            limit=args.max_issues,
            fetch_all=bool(args.fail_on_severity),
        )
        measures = (
            fetch_measures(
                host=args.host,
                token=token,
                project_key=args.project_key,
                pull_request=args.pull_request,
                branch=args.branch,
            )
            if args.fail_on_severity and args.pull_request
            else {}
        )
    except Exception as error:  # noqa: BLE001 - reporting must never mask the real failure
        print(f"::warning::Could not read the SonarCloud gate: {error}")
        if args.fail_on_severity:
            print("::error::No se pudo verificar el gate estricto de SonarCloud.")
            return 1
        return 0

    report = format_report(
        project_key=args.project_key,
        gate=gate,
        issues=issues[: args.max_issues],
        pull_request=args.pull_request,
        branch=args.branch,
    )
    args.output.write_text(report, encoding="utf-8")
    print(report)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(report)

    if args.fail_on_severity:
        print(
            "New code: "
            f"lines={measures.get('new_lines') or 'n/a'}, "
            f"lines_to_cover={measures.get('new_lines_to_cover') or 'n/a'}, "
            f"coverage={measures.get('new_coverage') or 'n/a'}, "
            f"duplication={measures.get('new_duplicated_lines_density') or 'n/a'}"
        )
        metric_failures = (
            measure_failures(
                measures,
                min_new_coverage=args.min_new_coverage,
                max_new_duplication=args.max_new_duplication,
                require_metrics=args.require_new_code_metrics,
            )
            if args.pull_request
            else []
        )
        if metric_failures:
            for failure in metric_failures:
                print(f"::error::{failure}.")
            return 1

        blocking = blocking_issues(issues, args.fail_on_severity)
        if blocking:
            print(
                f"::error::SonarCloud reporta {len(blocking)} issue(s) nuevos "
                f"de severidad {args.fail_on_severity} o superior."
            )
            for issue in blocking[:20]:
                component = str(issue.get("component", "")).split(":", 1)[-1]
                print(
                    f"  {component}:{issue.get('line', '—')} "
                    f"[{issue.get('severity')}] {issue.get('rule')} — {issue.get('message')}"
                )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
