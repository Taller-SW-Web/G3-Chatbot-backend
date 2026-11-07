"""Export the FastAPI OpenAPI schema to JSON (H4-A2)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "docs" / "openapi.json"


def build_openapi_schema() -> dict[str, Any]:
    """Load the FastAPI application and return its OpenAPI schema."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from src.infrastructure.main import app

    return app.openapi()


def render_openapi_json(schema: dict[str, Any] | None = None) -> str:
    """Serialize the OpenAPI schema deterministically as formatted JSON."""
    payload = schema if schema is not None else build_openapi_schema()
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def export_openapi(
    output_path: Path = DEFAULT_OUTPUT_PATH,
    schema: dict[str, Any] | None = None,
) -> Path:
    """Write the OpenAPI schema to ``output_path`` and return the resolved path."""
    resolved = output_path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(render_openapi_json(schema), encoding="utf-8")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the G3 Chatbot backend OpenAPI specification to JSON."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Destination path for openapi.json (default: docs/openapi.json).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify that the output file exists and matches the current OpenAPI schema.",
    )
    args = parser.parse_args(argv)

    schema = build_openapi_schema()
    output_path: Path = args.output

    if args.check:
        if not output_path.is_file():
            print(f"OpenAPI file not found: {output_path}", file=sys.stderr)
            return 1
        current = output_path.read_text(encoding="utf-8")
        expected = render_openapi_json(schema)
        if current != expected:
            print(
                f"OpenAPI file is out of date: {output_path}. "
                "Run `python scripts/export_openapi.py` to update it.",
                file=sys.stderr,
            )
            return 1
        print(f"OpenAPI schema is up to date ({len(schema.get('paths', {}))} paths): {output_path}")
        return 0

    written_path = export_openapi(output_path, schema=schema)
    print(f"Exported OpenAPI schema ({len(schema.get('paths', {}))} paths) to {written_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
