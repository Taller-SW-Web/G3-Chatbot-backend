"""Persistencia de referencias a Ventas; sin upload, worker ni commit propio.

El caso de uso valida pertenencia de conversación y devolución. La asociación
de evidencias y el INSERT de return_ref deben compartir transacción.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_postventa.domain.draft_retention import (
    CLAIM_DRAFT_CONTEXT_KEY,
    RETURN_DRAFT_CONTEXT_KEY,
    remove_expired_drafts,
)
from src.modules.mod_postventa.domain.references import NewEvidenceReference
from src.shared.infrastructure.db.models.conversation import Conversation
from src.shared.infrastructure.db.models.evidence import Evidence


class EvidencePostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def tipo_desde_ventas(tipo: str) -> str:
        """Normaliza el literal confirmado por G5 al valor local en inglés."""
        if tipo == "IMAGEN":
            return "IMAGE"
        if tipo == "DOCUMENTO":
            return "DOCUMENT"
        return tipo

    @staticmethod
    def tipo_hacia_ventas(tipo: str) -> str:
        """Usar al ensamblar evidencias[] de la solicitud a G5."""
        if tipo == "IMAGE":
            return "IMAGEN"
        if tipo in {"PDF", "DOCUMENT", "DOCUMENTO"}:
            return "DOCUMENTO"
        return tipo

    async def guardar(self, evidence: Evidence | NewEvidenceReference) -> Evidence:
        if not isinstance(evidence, Evidence):
            evidence = Evidence(
                conversation_id=evidence.conversation_id,
                type=evidence.type,
                url=evidence.url,
                original_file_name=evidence.original_file_name,
                size_bytes=evidence.size_bytes,
            )
        evidence.type = self.tipo_desde_ventas(evidence.type)
        self.session.add(evidence)
        await self.session.flush()
        return evidence

    async def listar_borrador(self, conversation_id: uuid.UUID) -> list[Evidence]:
        stmt = (
            select(Evidence)
            .where(
                Evidence.conversation_id == conversation_id,
                Evidence.return_id.is_(None),
            )
            .order_by(Evidence.created_at, Evidence.id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def listar_borrador_por_urls(
        self,
        urls: Sequence[str],
        customer_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
    ) -> list[Evidence]:
        """Drafts of this customer whose URL was sent in the confirmed return.

        Joins `conversation` so a URL copied from another customer never links.
        With `conversation_id`, only that conversation's drafts are candidates.
        """
        values = [url for url in dict.fromkeys(urls) if url]
        if not values:
            return []
        stmt = (
            select(Evidence)
            .join(Conversation, Conversation.id == Evidence.conversation_id)
            .where(
                Evidence.return_id.is_(None),
                Evidence.url.in_(values),
                Conversation.customer_id == customer_id,
            )
            .order_by(Evidence.created_at, Evidence.id)
        )
        if conversation_id is not None:
            stmt = stmt.where(Evidence.conversation_id == conversation_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def purgar_borradores(self, corte: datetime) -> int:
        """Delete evidence references still unlinked after the retention window.

        Ventas keeps the file; this only removes the local URL of an abandoned draft.
        Returns rows deleted and never commits: the job owns the unit of work.
        """
        stmt = delete(Evidence).where(
            Evidence.return_id.is_(None),
            Evidence.created_at < corte,
        )
        result = cast(CursorResult[Any], await self.session.execute(stmt))
        return int(result.rowcount or 0)

    async def purgar_borradores_contexto(self, ahora: datetime, limite: int = 1000) -> int:
        """Delete expired `postventa*Draft` keys from `conversation.context`.

        Locks the selected rows (SKIP LOCKED is safe across replicas) and removes
        only the keys owned by this module; returns how many keys were removed.
        """
        stmt = (
            select(Conversation)
            .where(
                or_(
                    Conversation.context.has_key(RETURN_DRAFT_CONTEXT_KEY),
                    Conversation.context.has_key(CLAIM_DRAFT_CONTEXT_KEY),
                )
            )
            .order_by(Conversation.updated_at)
            .limit(limite)
            .with_for_update(skip_locked=True)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        removed = 0
        for row in rows:
            context = dict(row.context) if isinstance(row.context, dict) else {}
            count = remove_expired_drafts(context, ahora)
            if count:
                row.context = context
                removed += count
        if removed:
            await self.session.flush()
        return removed

    async def asociar_devolucion(
        self, evidence_id: uuid.UUID, conversation_id: uuid.UUID, return_id: str
    ) -> Evidence | None:
        """Asocia un borrador; reintentos admitidos, reasignaciones rechazadas.

        None significa inexistente, de otra conversación o ya ligada a otra
        devolución. El UPDATE filtra atómicamente para evitar reasignaciones.
        """
        stmt = (
            update(Evidence)
            .where(
                Evidence.id == evidence_id,
                Evidence.conversation_id == conversation_id,
                or_(Evidence.return_id.is_(None), Evidence.return_id == return_id),
            )
            .values(return_id=return_id)
            .returning(Evidence)
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
