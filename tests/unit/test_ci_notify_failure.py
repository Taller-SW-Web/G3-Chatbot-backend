from __future__ import annotations

import json

from scripts.ci.notify_failure import MARKER, build_markdown, truncate_log


def test_build_markdown_lists_failed_jobs_and_logs() -> None:
    body = build_markdown(
        repo="Taller-SW-Web/G3-Chatbot-backend",
        run_id="42",
        run_url="https://github.com/Taller-SW-Web/G3-Chatbot-backend/actions/runs/42",
        reason="modules: mod_catalog",
        jobs=[
            {"id": 1, "name": "validate", "conclusion": "failure"},
            {"id": 2, "name": "unit-tests", "conclusion": "failure"},
        ],
        logs={"1": "ruff error", "2": "pytest failure"},
        needs_json=json.dumps(
            {
                "validate": {"result": "failure"},
                "unit-tests": {"result": "failure"},
                "sonar": {"result": "skipped"},
            }
        ),
    )
    assert body.startswith(MARKER)
    assert "CI falló" in body
    assert "| validate | failure |" in body
    assert "ruff error" in body
    assert "pytest failure" in body
    assert "sonar" not in body


def test_truncate_log_strips_ansi_and_backticks() -> None:
    rendered = truncate_log("\x1b[31merror\x1b[0m\n```python\nprint(1)\n```")
    assert "\x1b" not in rendered
    assert "```" not in rendered
    assert "~~~python" in rendered
