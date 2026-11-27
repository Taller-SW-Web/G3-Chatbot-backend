"""Detect drift between upstream module contracts and the local lock (H5-I1).

`tests/contract/contracts.lock.json` pins the repository, ref, path, version and
SHA-256 of every contract exercised by `tests/contract`. This script downloads
the same files at the pinned ref and compares hash, version and publication
state. It is intentionally read-only: the scheduled workflow reports drift, and
a human updates the lock plus fixtures in a normal PR.

Exit codes:
    0  every contract matches the lock (or `--update` rewrote it)
    1  at least one contract drifted
    2  the lock could not be read or an upstream file could not be fetched

Usage:
    python scripts/check_contract_drift.py
    python scripts/check_contract_drift.py --json
    python scripts/check_contract_drift.py --update
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "tests" / "contract" / "contracts.lock.json"
USER_AGENT = "g3-chatbot-contract-drift/1.0"
DEFAULT_TIMEOUT_SECONDS = 20.0
Fetcher = Callable[[str], bytes]

# Top-level `info.version` and `info.x-estado`; both live at two-space indent in
# the upstream OpenAPI files. `\s*` after x-estado covers the folded scalar used
# by G7 (`x-estado:` followed by the value on the next line).
_YAML_VERSION = re.compile(r"^ {2}version:\s*[\"']?([^\s\"'#]+)", re.MULTILINE)
_YAML_STATE = re.compile(r"^ {2}x-estado:\s*(\S+)", re.MULTILINE)
_MARKDOWN_VERSION = re.compile(
    r"^(?:versi[oó]n|version)\s*[:\-]\s*(\S+)", re.IGNORECASE | re.MULTILINE
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def extract_version(text: str, path: str) -> str | None:
    """Read the contract version from OpenAPI, JSON or Markdown."""
    suffix = Path(path).suffix.lower()
    if suffix in {".yaml", ".yml", ".json"}:
        match = _YAML_VERSION.search(text)
        return match.group(1) if match else None
    match = _MARKDOWN_VERSION.search(text)
    return match.group(1) if match else None


def extract_state(text: str) -> str | None:
    """Publication state (`x-estado`) when the contract declares one."""
    match = _YAML_STATE.search(text)
    return match.group(1) if match else None


def fetch_url(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def evaluate(lock: dict[str, Any], fetch: Fetcher = fetch_url) -> list[dict[str, Any]]:
    """Compare every locked contract against its upstream bytes."""
    results: list[dict[str, Any]] = []
    for contract in lock.get("contracts", []):
        payload = fetch(contract["url"])
        text = payload.decode("utf-8")
        actual_sha256 = sha256_bytes(payload)
        actual_version = extract_version(text, contract["path"])
        actual_state = extract_state(text)
        hash_match = actual_sha256 == contract.get("sha256")
        version_match = actual_version == contract.get("version")
        state_match = actual_state == contract.get("state")
        results.append(
            {
                "id": contract["id"],
                "module": contract.get("module"),
                "source": contract.get("source"),
                "ref": contract.get("ref"),
                "path": contract.get("path"),
                "url": contract["url"],
                "expected_sha256": contract.get("sha256"),
                "actual_sha256": actual_sha256,
                "hash_match": hash_match,
                "expected_version": contract.get("version"),
                "actual_version": actual_version,
                "version_match": version_match,
                "expected_state": contract.get("state"),
                "actual_state": actual_state,
                "state_match": state_match,
                "ok": hash_match and version_match and state_match,
            }
        )
    return results


def update_lock(lock: dict[str, Any], results: list[dict[str, Any]], path: Path) -> None:
    """Persist the upstream hashes/versions after a reviewed fixture update."""
    by_id = {result["id"]: result for result in results}
    for contract in lock.get("contracts", []):
        result = by_id[contract["id"]]
        contract["sha256"] = result["actual_sha256"]
        contract["version"] = result["actual_version"]
        contract["state"] = result["actual_state"]
    lock["updated_at"] = dt.date.today().isoformat()
    path.write_text(
        json.dumps(lock, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _print_human(lock: dict[str, Any], results: list[dict[str, Any]]) -> None:
    print(f"Contract drift check: {len(results)} contract(s) pinned in {lock.get('updated_at')}")
    for result in results:
        if result["ok"]:
            version = result["actual_version"] or "sin versión"
            state = f" ({result['actual_state']})" if result["actual_state"] else ""
            print(f"[OK]     {result['id']}: {version}{state} {result['actual_sha256'][:12]}")
            continue
        reasons: list[str] = []
        if not result["hash_match"]:
            reasons.append(
                f"sha256 {str(result['expected_sha256'])[:12]} -> "
                f"{result['actual_sha256'][:12]}"
            )
        if not result["version_match"]:
            reasons.append(
                f"versión {result['expected_version']!r} -> {result['actual_version']!r}"
            )
        if not result["state_match"]:
            reasons.append(
                f"estado {result['expected_state']!r} -> {result['actual_state']!r}"
            )
        print(f"[DRIFT]  {result['id']}: {'; '.join(reasons)}")
        print(f"         {result['url']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--json", action="store_true", help="imprime el reporte JSON")
    parser.add_argument(
        "--update",
        action="store_true",
        help="reescribe hashes/versiones del lock con los valores upstream",
    )
    args = parser.parse_args(argv)

    try:
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"No se pudo leer el lock {args.lock}: {exc}", file=sys.stderr)
        return 2

    fetcher = functools.partial(fetch_url, timeout=args.timeout)
    try:
        results = evaluate(lock, fetch=fetcher)
    except (OSError, UnicodeDecodeError, urllib.error.URLError) as exc:
        print(f"No se pudo descargar un contrato upstream: {exc}", file=sys.stderr)
        return 2

    if args.update:
        update_lock(lock, results, args.lock)
        _print_human(lock, results)
        print(f"Lock actualizado: {args.lock}")
        return 0

    if args.json:
        print(json.dumps({"contracts": results, "drift": not all(r["ok"] for r in results)}, indent=2))
    else:
        _print_human(lock, results)

    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
