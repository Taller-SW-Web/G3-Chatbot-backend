"""Gate de specs: un PR que toca modulos debe enlazar la spec que gobierna el cambio.

Uso (CI):
    python scripts/check_specs.py --base origin/development \
        --specs-repo specs-repo --body pr-body.md

Reglas:
- Si el diff no toca `src/modules/mod_*`, el gate pasa.
- Cada modulo tocado debe declarar `specs` en `modules.manifest.json`.
- La descripcion del PR debe incluir `openspec/specs/<capability>` de ese modulo.
- La spec enlazada debe existir en el checkout del repo de specs.

No valida el contenido de la spec (eso lo hace el CI de specs y la revision);
garantiza que el PR declara y enlaza la spec correcta.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "modules.manifest.json"
MODULE_RE = re.compile(r"^src/modules/(mod_[a-z_]+)/")


def changed_modules(base: str) -> set[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    modules: set[str] = set()
    for line in result.stdout.splitlines():
        match = MODULE_RE.match(line.strip())
        if match:
            modules.add(match.group(1).replace("_", "-"))
    return modules


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--specs-repo", required=True)
    parser.add_argument("--body", required=True)
    args = parser.parse_args()

    body = pathlib.Path(args.body).read_text(encoding="utf-8", errors="replace")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    by_id = {module["id"]: module for module in manifest["modules"]}
    modules = changed_modules(args.base)
    if not modules:
        print("OK: el PR no toca src/modules/mod_*")
        return 0

    errors: list[str] = []
    for module_id in sorted(modules):
        declared = by_id.get(module_id, {}).get("specs") or []
        if not declared:
            errors.append(f"{module_id}: no declara specs en modules.manifest.json")
            continue
        linked = [spec for spec in declared if f"openspec/specs/{spec}" in body]
        if not linked:
            errors.append(
                f"{module_id}: el PR no enlaza ninguna de sus specs "
                f"({', '.join(declared)})"
            )
            continue
        for spec in linked:
            spec_file = pathlib.Path(args.specs_repo) / "openspec" / "specs" / spec / "spec.md"
            if not spec_file.is_file():
                errors.append(f"{module_id}: la spec enlazada no existe: {spec_file}")

    if errors:
        print("Gate de specs: FALLO")
        for error in errors:
            print(f" - {error}")
        print("Añade en la descripción del PR: `Spec: openspec/specs/<capability>`")
        return 1
    print("OK: specs enlazadas y existentes para " + ", ".join(sorted(modules)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
