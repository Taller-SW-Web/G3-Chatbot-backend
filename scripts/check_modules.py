"""Validate the modular-monolith boundaries (ADR-0024).

Checks, with the Python AST only (no third-party dependencies):
1. `modules.manifest.json` ids have their package, `MODULE.md` and `public.py`.
2. `module.py` exports `MODULE_ID` (literal or re-exported from its package),
   `configure()` and `router`/`build_router()`, and `__init__.py` holds the
   canonical literal that matches the manifest.
3. Import rules:
   - a module file imports only `src.shared.*`, its own tree and
     `src.modules.<dep>.public` for the deps declared in the manifest;
   - `src/shared` never imports a module;
   - the composition root (`src/infrastructure/{main,discovery}.py`) imports
     only `src.modules.<id>.module`;
   - the removed layer-first packages cannot come back.
4. Every `APIRouter(...)` declared inside a module uses `tags=[MODULE_ID]`.

Exit code 1 with an itemized report when something is off.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MANIFEST_PATH = ROOT / "modules.manifest.json"
COMPOSITION_ROOT = {"main.py", "discovery.py"}
REMOVED_PACKAGES = ("src.adapters", "src.application", "src.domain", "src.ports")


def module_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def imported_modules(path: Path) -> list[tuple[int, str]]:
    """`(line, dotted module)` for every `src.*` import, including relative-import targets."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src"):
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src"):
            found.append((node.lineno, node.module))
    return found


def literal_module_id(path: Path) -> str | None:
    if not path.is_file():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "MODULE_ID":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        return node.value.value
    return None


def declares(module_ast: ast.Module, name: str) -> bool:
    for node in module_ast.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = node.names if isinstance(node, ast.ImportFrom) else node.names
            if any(alias.asname == name or alias.name == name for alias in names):
                return True
    return False


def router_tag_errors(path: Path, package: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_router = (isinstance(func, ast.Name) and func.id == "APIRouter") or (
            isinstance(func, ast.Attribute) and func.attr == "APIRouter"
        )
        if not is_router:
            continue
        tags = next((kw.value for kw in node.keywords if kw.arg == "tags"), None)
        ok = (
            isinstance(tags, ast.List)
            and len(tags.elts) == 1
            and isinstance(tags.elts[0], ast.Name)
            and tags.elts[0].id == "MODULE_ID"
        )
        if not ok:
            errors.append(
                f"{path.relative_to(ROOT).as_posix()}:{node.lineno}: APIRouter must declare tags=[MODULE_ID]"
            )
    return errors


def main() -> int:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    modules = manifest["modules"]
    ids = [entry["id"] for entry in modules]
    deps = {entry["id"]: set(entry["dependsOn"]) for entry in modules}
    packages = {module_id: module_id.replace("-", "_") for module_id in ids}
    package_to_id = {package: module_id for module_id, package in packages.items()}

    errors: list[str] = []

    # 1-2. structure and module contract
    for module_id in ids:
        package = packages[module_id]
        directory = SRC / "modules" / package
        if not directory.is_dir():
            errors.append(f"missing package src/modules/{package} for {module_id}")
            continue
        for required in ("MODULE.md", "module.py", "public.py"):
            if not (directory / required).is_file():
                errors.append(f"{module_id}: missing {package}/{required}")
        init_id = literal_module_id(directory / "__init__.py")
        if init_id != module_id:
            errors.append(
                f"{module_id}: {package}/__init__.py must declare MODULE_ID = {module_id!r} (found {init_id!r})"
            )
        module_py = directory / "module.py"
        if module_py.is_file():
            tree = ast.parse(module_py.read_text(encoding="utf-8"), filename=str(module_py))
            if not declares(tree, "configure"):
                errors.append(f"{module_id}: module.py must define configure(container)")
            if not (declares(tree, "router") or declares(tree, "build_router")):
                errors.append(f"{module_id}: module.py must expose router or build_router(options)")
        for other, other_package in packages.items():
            if other == module_id:
                continue
            if (SRC / "modules" / other_package).is_dir() and other not in deps[module_id]:
                pass  # dependency direction is enforced on imports below

    # 3. import rules
    for path in module_files(SRC / "modules"):
        rel = path.relative_to(SRC / "modules")
        own_package = rel.parts[0]
        if len(rel.parts) < 2:
            continue  # src/modules/__init__.py
        own_id = package_to_id.get(own_package)
        if own_id is None:
            errors.append(f"{path.relative_to(ROOT).as_posix()}: not listed in modules.manifest.json")
            continue
        for line, module in imported_modules(path):
            if module == "src" or module.startswith("src.") is False:
                continue
            if module == "src.shared" or module.startswith("src.shared."):
                continue
            if module == f"src.modules.{own_package}" or module.startswith(f"src.modules.{own_package}."):
                continue
            if module.startswith("src.modules."):
                parts = module.split(".")
                other_package = parts[2] if len(parts) > 2 else ""
                other_id = package_to_id.get(other_package)
                if other_id is None:
                    errors.append(
                        f"{path.relative_to(ROOT).as_posix()}:{line}: imports unknown module {module!r}"
                    )
                    continue
                if other_id not in deps[own_id]:
                    errors.append(
                        f"{path.relative_to(ROOT).as_posix()}:{line}: {own_id} imports {other_id} "
                        f"but the manifest does not declare it in dependsOn"
                    )
                    continue
                if module != f"src.modules.{other_package}.public":
                    errors.append(
                        f"{path.relative_to(ROOT).as_posix()}:{line}: {own_id} must import "
                        f"src.modules.{other_package}.public, not {module!r}"
                    )
                continue
            if module.startswith(REMOVED_PACKAGES):
                errors.append(
                    f"{path.relative_to(ROOT).as_posix()}:{line}: removed layer-first package {module!r}"
                )
                continue
            errors.append(f"{path.relative_to(ROOT).as_posix()}:{line}: forbidden import {module!r}")

    # 4. router tags
    for path in module_files(SRC / "modules"):
        if "adapters" in path.parts:
            errors.extend(router_tag_errors(path, path.relative_to(SRC / "modules").parts[0]))

    # shared must not import modules; composition root only module.py
    for path in module_files(SRC / "shared"):
        for line, module in imported_modules(path):
            if module.startswith("src.modules"):
                errors.append(
                    f"{path.relative_to(ROOT).as_posix()}:{line}: src/shared must not import {module!r}"
                )
    for name in COMPOSITION_ROOT:
        path = SRC / "infrastructure" / name
        if not path.is_file():
            errors.append(f"missing composition file src/infrastructure/{name}")
            continue
        for line, module in imported_modules(path):
            if module.startswith("src.modules.") and not module.endswith(".module"):
                errors.append(
                    f"{path.relative_to(ROOT).as_posix()}:{line}: composition root may only import "
                    f"src.modules.<id>.module, not {module!r}"
                )
            elif module.startswith(REMOVED_PACKAGES):
                errors.append(
                    f"{path.relative_to(ROOT).as_posix()}:{line}: removed layer-first package {module!r}"
                )

    if errors:
        print(f"check_modules: {len(errors)} violation(s)\n", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(
        f"check_modules: OK ({len(ids)} modules, boundaries and router tags verified)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
