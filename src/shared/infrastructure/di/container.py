"""Kernel wiring (ADR-0024).

`shared` builds only kernel-level providers. Module services are built by each
`src/modules/<id>/module.py`; the only cross-module shim left here is the
outbox processor, which the worker lifespan resolves from the capability
registry (registered by `mod-checkout`).
"""
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.infrastructure.di.capabilities import OUTBOX_PROCESSOR
from src.shared.infrastructure.di.registry import registry


def provide_outbox_processor(session: AsyncSession):
    """The outbox processor registered by `mod-checkout` (no module import here)."""
    return registry.resolve(OUTBOX_PROCESSOR, session)


__all__ = ["OUTBOX_PROCESSOR", "provide_outbox_processor"]
