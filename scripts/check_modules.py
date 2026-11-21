"""Validate the modular-monolith boundaries (ADR-0024).

Checks, with the Python AST only (no third-party dependencies):
1. `modules.manifest.json` ids have their package, `MODULE.md` and `public.py`.
2. `module.py` exports `MODULE_ID` (literal or re-exported from its package),
   `configure()` and `router`/`build_router()`, and `__init__.py` holds the
   canonical literal that matches the manifest.
3. Import rules:
   - a module file imports only `src.shared.*`, its own tree and
     `src.modules.<dep>.public` for the deps declared in the manifest;
   - relative imports are resolved before applying the rule;
   - `importlib.import_module` / `__import__` are rejected (a constant target
     is still validated, a dynamic one is a hard error);
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
COMPOSITION_ROOT = {"main.py", "discovery.py"}
REMOVED_PACKAGES = ("src.adapters", "src.application", "src.domain", "src.ports")
# Auto-discovery is the one place that must import a package name built at runtime.
DYNAMIC_IMPORT_ALLOWLIST = {"src/infrastructure/discovery.py"}


def module_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def _package_parts(path: Path, src_root: Path) -> list[str]:
    """Package that contains `path`, relative to `src_root` (`foo/__init__.py` -> foo)."""
    return list(path.resolve().relative_to(src_root.resolve()).with_suffix("").parts[:-1])


def _resolve_from(module: str | None, level: int, package: list[str]) -> str | None:
    """Resolve an `ImportFrom` target; `None` when the level escapes the source tree."""
    if level == 0:
        return module
    if level > len(package):
        return None
    base = package[: len(package) - (level - 1)]
    parts = [*base, *(module.split(".") if module else [])]
    return ".".join(parts)


def _relative_target(name: str, package: str | None, src_root: Path) -> str | None:
    """Resolve a constant name passed to `importlib.import_module`."""
    if not name.startswith(".") or not package:
        return name
    parts = package.split(".")
    if parts and parts[0] == "src":
        parts = parts[1:]
    level = len(name) - len(name.lstrip("."))
    remainder = name[level:]
    resolved = _resolve_from(remainder or None, level, parts)
    if resolved is None:
        return None
    return resolved if resolved.startswith("src.") else f"src.{resolved}"


def _dynamic_import_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names bound to `importlib` and to `from importlib import import_module`."""
    importlib_names: set[str] = set()
    import_module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_names.add(alias.asname or "importlib")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    import_module_names.add(alias.asname or "import_module")
    return importlib_names, import_module_names


def _dynamic_call_target(
    node: ast.Call,
    importlib_names: set[str],
    import_module_names: set[str],
    path: Path,
    src_root: Path,
) -> tuple[str | None, str | None]:
    """`(constant target, dynamic error)` for `importlib.import_module` / `__import__`."""
    func = node.func
    is_dynamic = False
    if isinstance(func, ast.Name):
        is_dynamic = func.id == "__import__" or func.id in import_module_names
    elif isinstance(func, ast.Attribute) and func.attr == "import_module":
        is_dynamic = (
            isinstance(func.value, ast.Name)
            and func.value.id in importlib_names
        ) or isinstance(func.value, ast.Attribute)
    if not is_dynamic:
        return None, None

    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(
        node.args[0].value, str
    ):
        return None, f"{node.lineno}: dynamic import target is not a constant; use a static import"

    name = node.args[0].value
    package = None
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        package = node.args[1].value if isinstance(node.args[1].value, str) else None
    for keyword in node.keywords:
        if keyword.arg == "package" and isinstance(keyword.value, ast.Constant):
            package = keyword.value.value if isinstance(keyword.value.value, str) else None
    if name.startswith("."):
        if not package:
            return None, f"{node.lineno}: relative dynamic import without a constant package"
        if package.startswith("src."):
            package = package
        else:
            package = f"src.{package}"
        target = _relative_target(name, package, src_root)
        if target is None:
            return None, f"{node.lineno}: dynamic import escapes src/"
    else:
        target = name
    return target, None


def scan_imports(
    path: Path, src_root: Path
) -> tuple[list[tuple[int, str]], list[str]]:
    """`(line, dotted module)` plus dynamic-import errors for every import in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package_parts(path, src_root)
    importlib_names, import_module_names = _dynamic_import_names(tree)
    found: list[tuple[int, str]] = []
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "src" or alias.name.startswith("src."):
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                module = _resolve_from(node.module, node.level, package)
                if module is None:
                    errors.append(
                        f"{node.lineno}: relative import escapes the source tree"
                    )
                    continue
                module = module if module.startswith("src") else f"src.{module}"
            else:
                module = node.module
            if module and (module == "src" or module.startswith("src.")):
                found.append((node.lineno, module))
        elif isinstance(node, ast.Call):
            target, error = _dynamic_call_target(
                node, importlib_names, import_module_names, path, src_root
            )
            if error:
                errors.append(error)
            elif target and (target == "src" or target.startswith("src.")):
                found.append((node.lineno, target))
    return found, errors


def imported_modules(path: Path, src_root: Path) -> list[tuple[int, str]]:
    """Static and constant-dynamic `src.*` imports, including relative targets."""
    found, _ = scan_imports(path, src_root)
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
            if any(alias.asname == name or alias.name == name for alias in node.names):
                return True
    return False


def router_tag_errors(path: Path, root: Path = ROOT) -> list[str]:
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
                f"{path.relative_to(root).as_posix()}:{node.lineno}: APIRouter must declare tags=[MODULE_ID]"
            )
    return errors


def validate(root: Path) -> list[str]:
    src = root / "src"
    manifest = json.loads((root / "modules.manifest.json").read_text(encoding="utf-8"))
    modules = manifest["modules"]
    ids = [entry["id"] for entry in modules]
    deps = {entry["id"]: set(entry["dependsOn"]) for entry in modules}
    packages = {module_id: module_id.replace("-", "_") for module_id in ids}
    package_to_id = {package: module_id for module_id, package in packages.items()}

    errors: list[str] = []

    # 1-2. structure and module contract
    for module_id in ids:
        package = packages[module_id]
        directory = src / "modules" / package
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

    # 3. import rules
    for path in module_files(src / "modules"):
        rel = path.relative_to(src / "modules")
        own_package = rel.parts[0]
        if len(rel.parts) < 2:
            continue  # src/modules/__init__.py
        own_id = package_to_id.get(own_package)
        if own_id is None:
            errors.append(f"{path.relative_to(root).as_posix()}: not listed in modules.manifest.json")
            continue
        imports, dynamic_errors = scan_imports(path, src)
        location = path.relative_to(root).as_posix()
        if location not in DYNAMIC_IMPORT_ALLOWLIST:
            errors.extend(f"{location}:{error}" for error in dynamic_errors)
        for line, module in imports:
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
                    errors.append(f"{location}:{line}: imports unknown module {module!r}")
                    continue
                if other_id not in deps[own_id]:
                    errors.append(
                        f"{location}:{line}: {own_id} imports {other_id} "
                        f"but the manifest does not declare it in dependsOn"
                    )
                    continue
                if module != f"src.modules.{other_package}.public":
                    errors.append(
                        f"{location}:{line}: {own_id} must import "
                        f"src.modules.{other_package}.public, not {module!r}"
                    )
                continue
            if module.startswith(REMOVED_PACKAGES):
                errors.append(f"{location}:{line}: removed layer-first package {module!r}")
                continue
            errors.append(f"{location}:{line}: forbidden import {module!r}")

    # 4. router tags
    for path in module_files(src / "modules"):
        if "adapters" in path.parts:
            errors.extend(router_tag_errors(path, root))

    # shared must not import modules; composition root only module.py
    for path in module_files(src / "shared"):
        imports, dynamic_errors = scan_imports(path, src)
        location = path.relative_to(root).as_posix()
        if location not in DYNAMIC_IMPORT_ALLOWLIST:
            errors.extend(f"{location}:{error}" for error in dynamic_errors)
        for line, module in imports:
            if module.startswith("src.modules"):
                errors.append(f"{location}:{line}: src/shared must not import {module!r}")
    for name in COMPOSITION_ROOT:
        path = src / "infrastructure" / name
        if not path.is_file():
            errors.append(f"missing composition file src/infrastructure/{name}")
            continue
        imports, dynamic_errors = scan_imports(path, src)
        location = path.relative_to(root).as_posix()
        if location not in DYNAMIC_IMPORT_ALLOWLIST:
            errors.extend(f"{location}:{error}" for error in dynamic_errors)
        for line, module in imports:
            if module.startswith("src.modules.") and not module.endswith(".module"):
                errors.append(
                    f"{location}:{line}: composition root may only import "
                    f"src.modules.<id>.module, not {module!r}"
                )
            elif module.startswith(REMOVED_PACKAGES):
                errors.append(f"{location}:{line}: removed layer-first package {module!r}")

    return errors


def main() -> int:
    errors = validate(ROOT)
    if errors:
        print(f"check_modules: {len(errors)} violation(s)\n", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    manifest = json.loads((ROOT / "modules.manifest.json").read_text(encoding="utf-8"))
    print(
        f"check_modules: OK ({len(manifest['modules'])} modules, boundaries and router tags verified)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
