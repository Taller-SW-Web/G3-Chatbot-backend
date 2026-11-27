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
) -> list[dict[str, Any]]:
    params: dict[str, str] = {
        "componentKeys": project_key,
        "resolved": "false",
        "ps": str(limit),
        "s": "SEVERITY",
        "asc": "false",
    }
    if pull_request:
        params["pullRequest"] = pull_request
    elif branch:
        params["branch"] = branch
    payload = _api_get(host, "/api/issues/search", params, token)
    return list(payload.get("issues", []))


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
    args = parser.parse_args(argv)

    token = os.getenv("SONAR_TOKEN")
    if not token:
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
        )
    except Exception as error:  # noqa: BLE001 - reporting must never mask the real failure
        print(f"::warning::Could not read the SonarCloud gate: {error}")
        return 0

    report = format_report(
        project_key=args.project_key,
        gate=gate,
        issues=issues,
        pull_request=args.pull_request,
        branch=args.branch,
    )
    args.output.write_text(report, encoding="utf-8")
    print(report)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
