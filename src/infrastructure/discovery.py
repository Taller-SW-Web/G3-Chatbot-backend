"""Module discovery for the composition root.

`src/modules/<package>/module.py` is the only entry point the app imports from
a module: it carries `MODULE_ID`, `router`/`build_router()` and `configure()`.
Nothing else in `src/shared` imports a module.

Capabilities are validated after every module ran `configure()`: a missing
required one aborts startup with the owner module in the message, and optional
ones are logged and exposed by `/health`.
"""
import importlib
import logging
import pkgutil
from types import ModuleType

import src.modules as modules_package
from src.shared.infrastructure.di.registry import CapabilityNotRegistered

logger = logging.getLogger(__name__)


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
    required: list[tuple[str, str]] = []
    optional: list[tuple[str, str]] = []
    for module in discover_modules():
        module_id = getattr(module, "MODULE_ID", None)
        if not module_id:
            raise CapabilityNotRegistered(
                f"{module.__name__} does not declare MODULE_ID; every module entry point must"
            )
        configure = getattr(module, "configure", None)
        if configure is None:
            raise CapabilityNotRegistered(
                f"{module_id}: module.py must define configure(container); "
                "silently skipping it hides missing capabilities"
            )
        configure(container)
        configured.append(module_id)
        required.extend((module_id, name) for name in getattr(module, "REQUIRED_CAPABILITIES", ()))
        optional.extend((module_id, name) for name in getattr(module, "OPTIONAL_CAPABILITIES", ()))

    missing_required = [
        f"{name} (required by {module_id})"
        for module_id, name in required
        if not container.is_registered(name)
    ]
    if missing_required:
        raise CapabilityNotRegistered(
            "missing required capabilities at startup: " + ", ".join(missing_required)
        )

    missing_optional = [
        f"{name} (optional for {module_id})"
        for module_id, name in optional
        if not container.is_registered(name)
    ]
    if missing_optional:
        logger.error(
            "Optional capabilities unavailable; affected features degrade: %s",
            ", ".join(missing_optional),
        )
    container.mark_optional_missing([name for _, name in optional if not container.is_registered(name)])
    return tuple(configured)
