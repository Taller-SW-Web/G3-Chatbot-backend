"""Run every blocking validator in one job and fail only at the end.

A single job keeps the setup cost low and shows the developer every error at
once instead of one job per tool. Each validator is a section of the log and of
``validation-report.txt``, which the workflow keeps as evidence for 90 days.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Validator:
    name: str
    command: tuple[str, ...]
    description: str = ""


@dataclass
class ValidationResult:
    validator: Validator
    exit_code: int
    duration_seconds: float
    output: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


VALIDATORS: tuple[Validator, ...] = (
    Validator("ruff", (sys.executable, "-m", "ruff", "check", "."), "Lint and import order"),
    Validator("mypy", (sys.executable, "-m", "mypy", "src"), "Static types"),
    Validator(
        "modules",
        (sys.executable, "scripts/check_modules.py"),
        "Module structure and import boundaries",
    ),
    Validator(
        "openapi",
        (sys.executable, "scripts/export_openapi.py", "--check"),
        "Committed OpenAPI matches the application",
    ),
    Validator(
        "import-app",
        (sys.executable, "-c", "from src.infrastructure.main import app"),
        "Wiring and capability discovery import cleanly",
    ),
)


def select_validators(names: Sequence[str] | None) -> list[Validator]:
    if not names:
        return list(VALIDATORS)
    wanted = {name.strip() for name in names if name.strip()}
    unknown = wanted - {validator.name for validator in VALIDATORS}
    if unknown:
        available = ", ".join(validator.name for validator in VALIDATORS)
        raise SystemExit(f"Unknown validator(s): {', '.join(sorted(unknown))}. Available: {available}")
    return [validator for validator in VALIDATORS if validator.name in wanted]


def run_validator(validator: Validator, *, cwd: Path = PROJECT_ROOT) -> ValidationResult:
    started = time.monotonic()
    environment = os.environ.copy()
    environment.setdefault("PYTHONPATH", ".")
    try:
        completed = subprocess.run(
            validator.command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        output = f"Timed out after 600s: {error}"
        return ValidationResult(
            validator=validator,
            exit_code=124,
            duration_seconds=time.monotonic() - started,
            output=output,
        )
    output = (completed.stdout or "") + (completed.stderr or "")
    return ValidationResult(
        validator=validator,
        exit_code=completed.returncode,
        duration_seconds=time.monotonic() - started,
        output=output.rstrip(),
    )


def render_report(results: Iterable[ValidationResult], *, generated_at: str) -> str:
    results = list(results)
    lines = [
        "# Validation report",
        "",
        f"- Generated: {generated_at}",
        f"- Commit: {os.getenv('GITHUB_SHA', 'local')}",
        f"- Result: {'PASS' if all(result.passed for result in results) else 'FAIL'}",
        "",
        "## Summary",
        "",
        "| Validator | Result | Duration |",
        "|---|---|---|",
    ]
    for result in results:
        status = "PASS" if result.passed else f"FAIL (exit {result.exit_code})"
        lines.append(
            f"| {result.validator.name} | {status} | {result.duration_seconds:.1f}s |"
        )
    for result in results:
        lines.extend(
            [
                "",
                f"## {result.validator.name}",
                "",
                f"_{result.validator.description}_",
                "",
                "```text",
                result.output or "(no output)",
                "```",
            ]
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "validation-report.txt",
        help="Path of the evidence report.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="Run only the named validator (repeatable).",
    )
    parser.add_argument("--list", action="store_true", help="List validators and exit.")
    args = parser.parse_args(argv)

    if args.list:
        for validator in VALIDATORS:
            print(f"{validator.name}: {validator.description}")
        return 0

    validators = select_validators(args.only)
    results: list[ValidationResult] = []
    for validator in validators:
        print(f"\n=== {validator.name} ===", flush=True)
        result = run_validator(validator)
        results.append(result)
        if result.output:
            print(result.output, flush=True)
        status = "PASS" if result.passed else f"FAIL (exit {result.exit_code})"
        print(f"[{status}] {validator.name} in {result.duration_seconds:.1f}s", flush=True)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = render_report(results, generated_at=generated_at)
    args.report.write_text(report, encoding="utf-8")

    failed = [result.validator.name for result in results if not result.passed]
    print("\nValidation summary")
    print(f"  passed: {len(results) - len(failed)}/{len(results)}")
    if failed:
        print(f"  failed: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
