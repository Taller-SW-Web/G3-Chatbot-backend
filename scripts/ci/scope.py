"""Compute the CI scope from the files a pull request or push changes.

The script is deliberately dependency-free so the ``scope`` job can run before
installing the project requirements. It maps changed paths to modules, closes
the module graph towards dependents and decides which expensive jobs are worth
running. If the base cannot be resolved it falls back to the full scope: failing
by excess is cheaper than skipping a gate by accident.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import deque
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = PROJECT_ROOT / "modules.manifest.json"

MODULE_PATH_RE = re.compile(
    r"^(?:src/modules/|tests/(?:unit|integration)/)(mod_[a-z0-9_]+)(?:/|$)"
)
INTEGRATION_ROOT = Path("tests/integration")
CORE_INTEGRATION_TESTS = (
    "tests/integration/test_gateway_kong.py",
    "tests/integration/test_outbox_consumer_rabbitmq.py",
    "tests/integration/test_rabbitmq_outbox_publisher.py",
)

FULL_SCOPE_PATTERNS = (
    "modules.manifest.json",
    "pytest.ini",
    ".coveragerc",
    "mypy.ini",
    "ruff.toml",
    "alembic.ini",
    "src/shared/**",
    "src/infrastructure/**",
    "tests/conftest.py",
    "tests/integration/conftest.py",
    "tests/integration/**/conftest.py",
    ".github/workflows/**",
    "scripts/ci/**",
    # The committed contract is not documentation: it is what the frontend
    # consumes and what export_openapi.py --check validates.
    "docs/openapi.json",
)
DEPENDENCY_PATTERNS = ("requirements*.txt",)
DOCKER_PATTERNS = ("Dockerfile", ".dockerignore", "gateway/**")
DOCS_PATTERNS = (
    "*.md",
    "docs/**",
    ".github/ISSUE_TEMPLATE/**",
    ".github/pull_request_template.md",
    ".github/CODEOWNERS",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
)
CODE_PATTERNS = (
    "src/**",
    "tests/**",
    "scripts/**",
)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch(path, pattern) for pattern in patterns)


def is_docs_path(path: str) -> bool:
    return _matches(path, DOCS_PATTERNS)


def is_dependency_path(path: str) -> bool:
    return _matches(path, DEPENDENCY_PATTERNS)


def is_docker_path(path: str) -> bool:
    return _matches(path, DOCKER_PATTERNS)


def is_code_path(path: str) -> bool:
    return _matches(path, CODE_PATTERNS)


def module_id_from_path(path: str) -> str | None:
    """Return the module id (``mod_auth``) affected by ``path``, if any."""
    match = MODULE_PATH_RE.match(path)
    if match is None:
        return None
    return match.group(1)


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, list[str]]:
    """Load the module graph as ``{module: [dependencies]}`` with snake_case ids."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    graph: dict[str, list[str]] = {}
    for module in payload.get("modules", []):
        module_id = str(module["id"]).replace("-", "_")
        graph[module_id] = [
            str(dependency).replace("-", "_")
            for dependency in module.get("dependsOn", [])
        ]
    return graph


def dependents_closure(
    changed_modules: Iterable[str], graph: Mapping[str, Sequence[str]]
) -> list[str]:
    """Return the changed modules plus every module that (transitively) depends on them."""
    reverse: dict[str, set[str]] = {module: set() for module in graph}
    for module, dependencies in graph.items():
        for dependency in dependencies:
            reverse.setdefault(dependency, set()).add(module)

    affected: set[str] = set()
    queue: deque[str] = deque(changed_modules)
    while queue:
        module = queue.popleft()
        if module in affected:
            continue
        affected.add(module)
        queue.extend(sorted(reverse.get(module, set()) - affected))
    return sorted(affected)


def integration_shards(
    affected_modules: Iterable[str],
    changed_files: Sequence[str],
    *,
    full_scope: bool,
    repo_root: Path = PROJECT_ROOT,
) -> list[str]:
    """Return the integration shards that have something to run."""
    available = sorted(
        path.name
        for path in (repo_root / INTEGRATION_ROOT).iterdir()
        if path.is_dir()
        and path.name != "core"
        and re.fullmatch(r"mod_[a-z0-9_]+", path.name) is not None
    )
    if full_scope:
        return [*available, "core"]

    shards = sorted(
        module for module in affected_modules if module in available
    )
    core_touched = any(
        path in CORE_INTEGRATION_TESTS
        or path == "tests/integration/__init__.py"
        or path.startswith("tests/integration/test_")
        for path in changed_files
    )
    if core_touched:
        shards.append("core")
    return shards


def _diff_files(base: str, repo_root: Path) -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=ACDMRTUB",
            f"{base}...HEAD",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"git diff failed for {base}")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _resolve_base(base: str | None, before: str | None, repo_root: Path) -> str | None:
    if base:
        completed = subprocess.run(
            ["git", "merge-base", base, "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        return completed.stdout.strip()
    if before and set(before) != {"0"}:
        completed = subprocess.run(
            ["git", "cat-file", "-e", f"{before}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return before
    return None


def build_scope(
    changed_files: Sequence[str],
    *,
    graph: Mapping[str, Sequence[str]],
    event: str,
    labels: Sequence[str],
    full: bool,
    repo_root: Path = PROJECT_ROOT,
) -> dict[str, object]:
    """Pure scope calculation, independent from git and GitHub."""
    changed = [path.replace("\\", "/") for path in changed_files]
    reasons: list[str] = []
    forced_by_label = "ci:full" in labels
    if not changed and event in {"pull_request", "push"}:
        # An empty diff in a PR/push means the base resolution or the diff
        # failed silently. Fail closed instead of skipping every gate.
        full = True
        reasons.append("empty diff on PR/push")

    docs_only = (
        bool(changed)
        and all(is_docs_path(path) for path in changed)
        and not any(_matches(path, FULL_SCOPE_PATTERNS) for path in changed)
    )
    has_dependencies = any(is_dependency_path(path) for path in changed)
    has_ci = any(
        path.startswith(".github/workflows/") or path.startswith("scripts/ci/")
        for path in changed
    )
    has_shared = any(path.startswith("src/shared/") for path in changed)
    has_config = any(_matches(path, FULL_SCOPE_PATTERNS) for path in changed)
    has_docker = any(is_docker_path(path) for path in changed)
    has_unknown = any(
        not is_docs_path(path)
        and not is_code_path(path)
        and not is_docker_path(path)
        and not _matches(path, FULL_SCOPE_PATTERNS)
        for path in changed
    )
    has_code = any(is_code_path(path) for path in changed)

    if full:
        reasons.append("forced by --full or ci:full label")
    if has_ci:
        reasons.append("CI workflow or script changed")
    if has_shared:
        reasons.append("shared code changed")
    if has_config:
        reasons.append("root build/test configuration changed")
    if has_dependencies:
        reasons.append("dependency files changed")
    if has_unknown:
        reasons.append("unmapped file changed")

    full_scope = bool(
        full
        or forced_by_label
        or not changed
        or has_ci
        or has_shared
        or has_config
        or has_dependencies
        or has_unknown
    )

    if not changed and full_scope:
        mode = "code"
        reasons.append("scheduled or dispatched full run")
    elif not changed:
        mode = "none"
        reasons.append("no changed files")
    elif docs_only and not full_scope:
        mode = "docs"
        reasons.append("docs only")
    elif has_dependencies:
        mode = "deps"
    elif has_ci:
        mode = "ci"
    elif has_docker and not has_code:
        mode = "infra"
    else:
        mode = "code"

    changed_modules = sorted(
        {
            module
            for module in (module_id_from_path(path) for path in changed)
            if module is not None
        }
    )
    affected = (
        sorted(graph)
        if full_scope
        else dependents_closure(changed_modules, graph)
    )
    if changed_modules:
        reasons.append("modules: " + ", ".join(changed_modules))
    if full_scope and not changed_modules and mode in {"code", "deps", "ci"}:
        reasons.append("full module scope")

    shards = integration_shards(
        affected,
        changed,
        full_scope=full_scope,
        repo_root=repo_root,
    )
    is_pull_request = event == "pull_request"
    force_expensive = not is_pull_request or forced_by_label

    run_validate = mode in {"code", "deps", "ci"}
    run_unit = mode in {"code", "deps", "ci"}
    run_integration = run_unit and bool(shards)
    run_docker = mode in {"code", "deps", "infra"} or has_docker
    run_security = (
        mode == "deps"
        or forced_by_label
        or event in {"schedule", "workflow_dispatch"}
    )
    run_dast = force_expensive and mode in {"code", "deps", "ci"}
    run_gateway = (
        force_expensive and mode in {"code", "deps", "ci", "infra"}
    ) or any(
        path.startswith("gateway/")
        or path == "tests/integration/test_gateway_kong.py"
        for path in changed
    )
    run_sonar = mode in {"code", "deps", "ci"}
    run_specs = is_pull_request
    run_dependency_review = is_pull_request

    return {
        "mode": mode,
        "full": full_scope,
        "modules": affected,
        "integration_shards": shards,
        "run_validate": run_validate,
        "run_unit": run_unit,
        "run_integration": run_integration,
        "run_docker": run_docker,
        "run_security": run_security,
        "run_dast": run_dast,
        "run_gateway": run_gateway,
        "run_sonar": run_sonar,
        "run_specs": run_specs,
        "run_dependency_review": run_dependency_review,
        "changed_count": len(changed),
        "reason": "; ".join(reasons) if reasons else "no scope triggers",
    }


def write_github_output(path: Path, scope: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for key, value in scope.items():
            rendered = json.dumps(value) if isinstance(value, list) else value
            if isinstance(rendered, bool):
                rendered = str(rendered).lower()
            handle.write(f"{key}={rendered}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="Git ref of the target branch (pull request).")
    parser.add_argument("--before", help="Previous commit SHA (push).")
    parser.add_argument("--event", default="other", help="GitHub event name.")
    parser.add_argument("--labels", default="", help="Comma-separated pull request labels.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--full", action="store_true", help="Force the full scope.")
    parser.add_argument("--github-output", type=Path, help="Path to $GITHUB_OUTPUT.")
    parser.add_argument("--json", action="store_true", help="Print the scope as JSON.")
    args = parser.parse_args(argv)

    labels = [label for label in args.labels.split(",") if label]
    event = args.event
    full = args.full or event in {"schedule", "workflow_dispatch"}

    if full:
        changed: list[str] = []
    else:
        resolved_base = _resolve_base(args.base, args.before, args.repo_root)
        if resolved_base is None:
            full = True
            changed = []
        else:
            try:
                changed = _diff_files(resolved_base, args.repo_root)
            except RuntimeError as error:
                print(f"::warning::scope fallback to full: {error}", file=sys.stderr)
                full = True
                changed = []

    scope = build_scope(
        changed,
        graph=load_manifest(args.manifest),
        event=event,
        labels=labels,
        full=full,
        repo_root=args.repo_root,
    )
    scope["reason"] = str(scope["reason"])
    if args.github_output:
        write_github_output(args.github_output, scope)
    payload = json.dumps(scope, indent=2, sort_keys=True)
    if args.json:
        print(payload)
    else:
        print("CI scope")
        for key, value in scope.items():
            print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
