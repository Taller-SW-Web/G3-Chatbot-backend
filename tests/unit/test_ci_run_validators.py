from __future__ import annotations

from scripts.ci.run_validators import (
    ValidationResult,
    Validator,
    render_report,
    select_validators,
)


def test_select_validators_rejects_unknown_names() -> None:
    try:
        select_validators(["nope"])
    except SystemExit as error:
        assert "Unknown validator" in str(error)
    else:  # pragma: no cover - the branch above must always raise
        raise AssertionError("select_validators should reject unknown names")


def test_render_report_marks_each_result() -> None:
    ok = ValidationResult(
        validator=Validator("ruff", ("ruff", "check", ".")),
        exit_code=0,
        duration_seconds=0.2,
        output="All checks passed",
    )
    failed = ValidationResult(
        validator=Validator("mypy", ("mypy", "src")),
        exit_code=1,
        duration_seconds=1.5,
        output="error: incompatible type",
    )
    report = render_report([ok, failed], generated_at="2026-11-27T00:00:00Z")
    assert "| ruff | PASS | 0.2s |" in report
    assert "| mypy | FAIL (exit 1) | 1.5s |" in report
    assert "## mypy" in report
    assert "error: incompatible type" in report
