"""Regression tests for the module-boundary gate (ADR-0024, H-06).

The checker must resolve relative imports and reject dynamic imports, so a
module cannot bypass the manifest by writing `from ...other.public import x`
or `importlib.import_module("src.modules.other.internal")`.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

CHECKER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_modules.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_modules", CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


check_modules = _load_checker()


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _module_tree(root: Path) -> None:
    manifest = {
        "version": 1,
        "modules": [
            {"id": "mod-a", "name": "A", "owner": "@a", "dependsOn": []},
            {"id": "mod-b", "name": "B", "owner": "@b", "dependsOn": []},
        ],
    }
    _write(root / "modules.manifest.json", json.dumps(manifest))
    _write(root / "src" / "shared" / "__init__.py")
    _write(root / "src" / "infrastructure" / "__init__.py")
    _write(root / "src" / "infrastructure" / "main.py")
    _write(root / "src" / "infrastructure" / "discovery.py")
    for module_id, package in (("mod-a", "mod_a"), ("mod-b", "mod_b")):
        base = root / "src" / "modules" / package
        _write(base / "__init__.py", f'MODULE_ID = "{module_id}"\n')
        _write(base / "MODULE.md", f"# {module_id}\n")
        _write(base / "public.py")
        _write(
            base / "module.py",
            f'from src.modules.{package} import MODULE_ID\n\n'
            "def configure(container):\n"
            "    pass\n\n"
            "router = None\n",
        )


def test_relative_cross_module_import_is_rejected(tmp_path):
    _module_tree(tmp_path)
    _write(
        tmp_path / "src" / "modules" / "mod_a" / "application" / "cross.py",
        "from ...mod_b.public import Thing\n",
    )

    errors = check_modules.validate(tmp_path)

    assert any("mod-a imports mod-b" in error for error in errors), errors


def test_constant_dynamic_import_is_validated_as_an_import(tmp_path):
    _module_tree(tmp_path)
    _write(
        tmp_path / "src" / "modules" / "mod_a" / "application" / "dynamic.py",
        "import importlib\n\n"
        'importlib.import_module("src.modules.mod_b.public")\n',
    )

    errors = check_modules.validate(tmp_path)

    assert any("mod-a imports mod-b" in error for error in errors), errors


def test_non_constant_dynamic_import_is_a_hard_error(tmp_path):
    _module_tree(tmp_path)
    _write(
        tmp_path / "src" / "modules" / "mod_a" / "application" / "dynamic.py",
        "import importlib\n\n"
        'target = "src.modules.mod_b.public"\n'
        "importlib.import_module(target)\n",
    )

    errors = check_modules.validate(tmp_path)

    assert any("dynamic import target is not a constant" in error for error in errors), errors


def test_own_relative_import_stays_allowed(tmp_path):
    _module_tree(tmp_path)
    _write(tmp_path / "src" / "modules" / "mod_a" / "domain" / "__init__.py")
    _write(tmp_path / "src" / "modules" / "mod_a" / "domain" / "value.py", "VALUE = 1\n")
    _write(
        tmp_path / "src" / "modules" / "mod_a" / "application" / "own.py",
        "from ..domain.value import VALUE\n",
    )

    assert check_modules.validate(tmp_path) == []


def test_repository_tree_passes_its_own_gate():
    assert check_modules.validate(CHECKER_PATH.parents[1]) == []
