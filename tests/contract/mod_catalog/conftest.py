"""Fixtures for the G6 contract tests (pinned OpenAPI 0.7.0-rc.3)."""
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    def _load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return _load
