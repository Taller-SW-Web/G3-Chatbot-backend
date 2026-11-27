"""The drift checker and the committed lock must agree with the fixtures."""
import hashlib
import json
from pathlib import Path

import scripts.check_contract_drift as drift

ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / "tests" / "contract" / "contracts.lock.json"
YAML_PAYLOAD = (
    b"openapi: 3.1.0\n"
    b"info:\n"
    b"  title: Test contract\n"
    b"  version: 1.2.0\n"
    b"  x-estado: revision-local-no-publicada\n"
)


def _contract(payload: bytes, **overrides) -> dict:
    contract = {
        "id": "g7-test",
        "module": "shared",
        "source": "Taller-SW-Web/Modulo-de-Seguridad",
        "ref": "0" * 40,
        "path": "specs/contrato-api/openapi.yaml",
        "version": "1.2.0",
        "state": "revision-local-no-publicada",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "url": "https://example.test/openapi.yaml",
    }
    contract.update(overrides)
    return contract


def _lock(payload: bytes, **overrides) -> dict:
    return {
        "version": 1,
        "updated_at": "2026-11-26",
        "contracts": [_contract(payload, **overrides)],
    }


def test_matching_hash_version_and_state_is_ok():
    results = drift.evaluate(_lock(YAML_PAYLOAD), fetch=lambda url: YAML_PAYLOAD)

    assert results[0]["ok"] is True
    assert results[0]["actual_version"] == "1.2.0"
    assert results[0]["actual_state"] == "revision-local-no-publicada"


def test_version_and_hash_changes_are_reported_as_drift():
    changed = YAML_PAYLOAD.replace(b"1.2.0", b"1.3.0")

    results = drift.evaluate(_lock(YAML_PAYLOAD), fetch=lambda url: changed)

    assert results[0]["ok"] is False
    assert results[0]["hash_match"] is False
    assert results[0]["version_match"] is False
    assert results[0]["actual_version"] == "1.3.0"


def test_g2_markdown_without_version_is_still_tracked_by_hash():
    markdown = b"# Contrato de API\n\nSin linea de version.\n"
    lock = _lock(
        markdown,
        path="integraciones/api-contract.md",
        version=None,
        state=None,
    )

    results = drift.evaluate(lock, fetch=lambda url: markdown)

    assert results[0]["ok"] is True
    assert results[0]["actual_version"] is None


def test_cli_returns_one_on_drift(monkeypatch, tmp_path, capsys):
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(_lock(YAML_PAYLOAD)), encoding="utf-8")
    changed = YAML_PAYLOAD.replace(b"1.2.0", b"1.3.0")
    monkeypatch.setattr(drift, "fetch_url", lambda url, timeout=None: changed)

    assert drift.main(["--lock", str(lock_path)]) == 1
    assert "[DRIFT]" in capsys.readouterr().out


def test_update_rewrites_hash_and_version(monkeypatch, tmp_path):
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(
        json.dumps(_lock(YAML_PAYLOAD, version="1.0.0")), encoding="utf-8"
    )
    changed = YAML_PAYLOAD.replace(b"1.2.0", b"1.3.0")
    monkeypatch.setattr(drift, "fetch_url", lambda url, timeout=None: changed)

    assert drift.main(["--lock", str(lock_path), "--update"]) == 0

    updated = json.loads(lock_path.read_text(encoding="utf-8"))
    assert updated["contracts"][0]["version"] == "1.3.0"
    assert updated["contracts"][0]["sha256"] == hashlib.sha256(changed).hexdigest()


def test_committed_fixtures_that_exist_match_the_lock():
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    checked = 0
    for entry in lock["contracts"]:
        fixture = ROOT / entry["fixture"]
        if not fixture.is_file():
            # Los PRs de módulo agregan su fixture por separado; el PR de
            # plataforma solo puede comprobar los que ya están en su rama.
            continue
        # Git normaliza a CRLF en algunos checkouts Windows; el contrato upstream
        # y el hash del lock se calculan sobre LF.
        payload = fixture.read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"], entry["id"]
        checked += 1

    assert checked >= 2  # G7 y G5 ventas viven en este PR.
