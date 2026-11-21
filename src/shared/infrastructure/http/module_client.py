"""Base URL + timeout wiring for an external module client (ADR-0017).

Switching mock<->real is config-only: point `*_BASE_URL` at Prism/respx or the
real module. The typed clients are built on top of this.
"""
from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.http.base_client import BaseModuleClient


def provide_module_client(module: str, settings: Settings | None = None) -> BaseModuleClient:
    settings = settings or get_settings()
    wiring = {
        "security": (settings.security_base_url, settings.security_timeout_seconds),
        "products": (settings.products_base_url, settings.products_timeout_seconds),
        "sales": (settings.sales_base_url, settings.sales_timeout_seconds),
        "dispatch": (settings.dispatch_base_url, settings.dispatch_timeout_seconds),
    }
    try:
        base_url, timeout_seconds = wiring[module]
    except KeyError:
        raise ValueError(f"unknown module {module!r} (security/products/sales/dispatch)") from None
    client = BaseModuleClient(base_url=base_url, timeout_seconds=timeout_seconds)
    client.module = module
    return client


__all__ = ["provide_module_client"]
