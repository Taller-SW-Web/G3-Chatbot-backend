"""Backwards-compatibility shim for require_customer (H4-S6)."""
from src.shared.infrastructure.http.dependencies.auth_guard import require_customer

__all__ = ["require_customer"]
