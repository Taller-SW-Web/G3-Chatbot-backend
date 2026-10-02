"""Persistencia de referencias a Ventas; sin upload, worker ni commit propio.

El caso de uso valida pertenencia de conversación y devolución. La asociación
de evidencias y el INSERT de return_ref deben compartir transacción.
"""

import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.evidence import Evidence


class EvidencePostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def tipo_desde_ventas(tipo: str) -> str:
        """Normaliza el literal confirmado por Ventas al valor local en inglés.

        No inventa una traducción para PDF ni para nuevos tipos del proveedor.
        """
        return "IMAGE" if tipo == "IMAGEN" else tipo

    @staticmethod
    def tipo_hacia_ventas(tipo: str) -> str:
        """Usar al ensamblar evidencias[] de la solicitud a Ventas."""
        return "IMAGEN" if tipo == "IMAGE" else tipo

    async def guardar(self, evidence: Evidence) -> Evidence:
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
