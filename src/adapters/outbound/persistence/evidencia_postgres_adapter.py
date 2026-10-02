"""Persistencia de referencias a Ventas; sin upload, worker ni commit propio.

El caso de uso valida pertenencia de conversación y devolución. La asociación
de evidencias y el INSERT de devolucion_ref deben compartir transacción.
"""

import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.evidencia import Evidencia


class EvidenciaPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, evidencia: Evidencia) -> Evidencia:
        self.session.add(evidencia)
        await self.session.flush()
        return evidencia

    async def listar_borrador(self, conversacion_id: uuid.UUID) -> list[Evidencia]:
        stmt = (
            select(Evidencia)
            .where(
                Evidencia.conversacion_id == conversacion_id,
                Evidencia.devolucion_id.is_(None),
            )
            .order_by(Evidencia.creado_en, Evidencia.id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def asociar_devolucion(
        self, evidencia_id: uuid.UUID, conversacion_id: uuid.UUID, devolucion_id: str
    ) -> Evidencia | None:
        """Asocia un borrador; reintentos admitidos, reasignaciones rechazadas.

        None significa inexistente, de otra conversación o ya ligada a otra
        devolución. El UPDATE filtra atómicamente para evitar reasignaciones.
        """
        stmt = (
            update(Evidencia)
            .where(
                Evidencia.id == evidencia_id,
                Evidencia.conversacion_id == conversacion_id,
                or_(Evidencia.devolucion_id.is_(None), Evidencia.devolucion_id == devolucion_id),
            )
            .values(devolucion_id=devolucion_id)
            .returning(Evidencia)
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
