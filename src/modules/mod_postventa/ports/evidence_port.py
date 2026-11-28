"""Outbound port for local evidence references (SPEC-21).

The service only needs to persist uploaded references, find drafts by URL and
attach them to a return. The PostgreSQL adapter implements the SQL; tests and
the fake mode can provide an in-memory implementation.
"""
import uuid
from collections.abc import Sequence
from typing import Any, Protocol


class EvidencePort(Protocol):
    async def guardar(self, evidence: Any) -> Any:
        """Persist one reference to a file hosted by Ventas."""
        ...

    async def listar_borrador_por_urls(
        self,
        urls: Sequence[str],
        customer_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
    ) -> list[Any]:
        """Unlinked drafts of this customer matching `urls`.

        When `conversation_id` is provided, only rows of that conversation are
        candidates; otherwise the caller deduplicates by URL.
        """
        ...

    async def asociar_devolucion(
        self, evidence_id: uuid.UUID, conversation_id: uuid.UUID, return_id: str
    ) -> Any | None:
        """Attach one draft to `return_id`; None when it is not attachable."""
        ...
