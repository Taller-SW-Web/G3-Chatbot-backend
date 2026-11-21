"""Module discovery for the composition root.

`src/modules/<package>/module.py` is the only entry point the app imports from
a module: it carries `MODULE_ID`, `router`/`build_router()` and `configure()`.
Nothing else in `src/shared` imports a module.
"""
import importlib
import pkgutil
from types import ModuleType

import src.modules as modules_package


def discover_modules() -> list[ModuleType]:
    """Every `src.modules.<package>.module`, ordered by package name (stable OpenAPI)."""
    found: list[ModuleType] = []
    for info in sorted(pkgutil.iter_modules(modules_package.__path__), key=lambda item: item.name):
        if not info.ispkg:
            continue
        found.append(importlib.import_module(f"src.modules.{info.name}.module"))
    return found


def configure_modules(container) -> tuple[str, ...]:
    """Call `configure(container)` on every discovered module; returns the module ids."""
    configured: list[str] = []
    for module in discover_modules():
        configure = getattr(module, "configure", None)
        if configure is not None:
            configure(container)
        configured.append(module.MODULE_ID)
    return tuple(configured)
