"""Publish a pull request comment when the CI pipeline fails.

The comment lists the failed jobs, links the run and includes a short excerpt of
each failed log. It is idempotent: the marker lets the next failure update the
same comment instead of filling the pull request with duplicates.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

MARKER = "<!-- g3-ci-failure -->"
MAX_COMMENT_CHARS = 55_000
MAX_LOG_LINES = 120
MAX_LOG_CHARS = 12_000
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FAILED_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required"}


def _api_base() -> str:
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    if server == "https://github.com":
        return "https://api.github.com"
    return f"{server}/api/v3"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "g3-chatbot-ci",
    }


def _request_json(
    method: str, url: str, token: str, payload: Mapping[str, Any] | None = None
) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers=_headers(token)
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        return error.code, body


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward the GitHub token to the signed storage host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            old_origin = urlsplit(req.full_url)
            new_origin = urlsplit(newurl)
            if (old_origin.scheme, old_origin.netloc) != (
                new_origin.scheme,
                new_origin.netloc,
            ):
                redirected.remove_header("Authorization")
        return redirected


def _request_text(url: str, token: str) -> str:
    request = urllib.request.Request(url, headers=_headers(token))
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(request, timeout=60) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return ""


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def truncate_log(text: str) -> str:
    text = strip_ansi(text).replace("```", "~~~").rstrip()
    lines = text.splitlines()[-MAX_LOG_LINES:]
    rendered = "\n".join(lines)
    if len(rendered) > MAX_LOG_CHARS:
        rendered = rendered[-MAX_LOG_CHARS:]
        rendered = "…(recortado)\n" + rendered
    return rendered or "(sin salida)"


def _needs_failures(needs_json: str) -> list[str]:
    try:
        needs = json.loads(needs_json or "{}")
    except json.JSONDecodeError:
        return []
    failed = []
    for name, payload in needs.items():
        if isinstance(payload, dict) and payload.get("result") in FAILED_CONCLUSIONS:
            failed.append(name)
    return sorted(failed)


def build_markdown(
    *,
    repo: str,
    run_id: str,
    run_url: str,
    reason: str,
    jobs: list[Mapping[str, Any]],
    logs: Mapping[str, str],
    needs_json: str = "",
) -> str:
    failed_names = [str(job.get("name", "")) for job in jobs]
    failed_names.extend(
        name for name in _needs_failures(needs_json) if name not in failed_names
    )
    lines = [
        MARKER,
        "## CI falló",
        "",
        f"**Run:** {run_url}",
        f"**Alcance:** {reason or 'no reportado'}",
        "",
        "| Job | Conclusión |",
        "|---|---|",
    ]
    for job in jobs:
        lines.append(
            "| {name} | {conclusion} |".format(
                name=str(job.get("name", "")).replace("|", "\\|"),
                conclusion=str(job.get("conclusion", "")),
            )
        )
    if not jobs:
        lines.append("| (sin jobs fallidos reportados por la API) | — |")
    if len(failed_names) > len(jobs):
        lines.extend(["", "Jobs fallidos según `needs`: " + ", ".join(f"`{name}`" for name in failed_names)])

    for job in jobs:
        name = str(job.get("name", "job"))
        log = logs.get(str(job.get("id")), "")
        if not log:
            continue
        lines.extend(
            [
                "",
                f"<details><summary>Log de {name}</summary>",
                "",
                "```text",
                truncate_log(log),
                "```",
                "",
                "</details>",
            ]
        )

    body = "\n".join(lines) + "\n"
    if len(body) > MAX_COMMENT_CHARS:
        body = body[: MAX_COMMENT_CHARS - 200] + "\n\n…(comentario recortado por tamaño)\n"
    return body


def _failed_jobs(repo: str, run_id: str, token: str) -> list[dict[str, Any]]:
    status, payload = _request_json(
        "GET",
        f"{_api_base()}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100",
        token,
    )
    if status != 200 or not isinstance(payload, dict):
        print(f"::warning::No se pudieron leer los jobs del run ({status}).")
        return []
    return [
        job
        for job in payload.get("jobs", [])
        if str(job.get("conclusion", "")) in FAILED_CONCLUSIONS
    ]


def _job_logs(repo: str, jobs: list[Mapping[str, Any]], token: str) -> dict[str, str]:
    logs: dict[str, str] = {}
    for job in jobs:
        job_id = job.get("id")
        if job_id is None:
            continue
        logs[str(job_id)] = _request_text(
            f"{_api_base()}/repos/{repo}/actions/jobs/{job_id}/logs", token
        )
    return logs


def _find_comment(repo: str, pr: str, token: str) -> int | None:
    for page in range(1, 6):
        status, payload = _request_json(
            "GET",
            f"{_api_base()}/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}",
            token,
        )
        if status != 200 or not isinstance(payload, list):
            return None
        for comment in payload:
            author = comment.get("user") or {}
            is_bot = author.get("login") == "github-actions[bot]" or author.get(
                "type"
            ) == "Bot"
            if is_bot and MARKER in str(comment.get("body", "")):
                return int(comment["id"])
        if len(payload) < 100:
            return None
    return None


def _upsert_comment(repo: str, pr: str, token: str, body: str) -> bool:
    comment_id = _find_comment(repo, pr, token)
    if comment_id is None:
        status, _ = _request_json(
            "POST",
            f"{_api_base()}/repos/{repo}/issues/{pr}/comments",
            token,
            {"body": body},
        )
    else:
        status, _ = _request_json(
            "PATCH",
            f"{_api_base()}/repos/{repo}/issues/comments/{comment_id}",
            token,
            {"body": body},
        )
        if status not in {200, 201}:
            # A stale marker owned by another actor must not silence the bot.
            status, _ = _request_json(
                "POST",
                f"{_api_base()}/repos/{repo}/issues/{pr}/comments",
                token,
                {"body": body},
            )
    if status not in {200, 201}:
        print(f"::warning::No se pudo publicar el comentario en el PR ({status}).")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-id", default=os.getenv("GITHUB_RUN_ID", ""))
    parser.add_argument("--pr", default=os.getenv("PR_NUMBER", ""))
    parser.add_argument("--head-repo", default=os.getenv("HEAD_REPO", ""))
    parser.add_argument("--reason", default=os.getenv("SCOPE_REASON", ""))
    parser.add_argument("--needs-json", default=os.getenv("NEEDS_JSON", ""))
    parser.add_argument("--output", type=Path, default=Path("ci-failure.md"))
    args = parser.parse_args(argv)

    token = os.getenv("GITHUB_TOKEN", "")
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    run_url = f"{server}/{args.repo}/actions/runs/{args.run_id}"

    jobs: list[dict[str, Any]] = []
    logs: dict[str, str] = {}
    if token and args.repo and args.run_id:
        jobs = _failed_jobs(args.repo, args.run_id, token)
        logs = _job_logs(args.repo, jobs, token)

    body = build_markdown(
        repo=args.repo,
        run_id=args.run_id,
        run_url=run_url,
        reason=args.reason,
        jobs=jobs,
        logs=logs,
        needs_json=args.needs_json,
    )
    args.output.write_text(body, encoding="utf-8")
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(body)

    if not token or not args.pr:
        print("No hay token o PR: el reporte queda en el job summary.")
        return 0
    if args.head_repo and args.head_repo != args.repo:
        print("PR desde un fork: el token es de solo lectura; se omite el comentario.")
        return 0
    _upsert_comment(args.repo, args.pr, token, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
