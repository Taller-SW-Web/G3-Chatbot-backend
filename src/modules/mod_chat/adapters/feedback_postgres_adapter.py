"""Adapter de sugerencias y bugs (SPEC-24)."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_chat.domain.feedback import NewFeedbackSuggestion
from src.shared.infrastructure.db.models.feedback import FeedbackSuggestion


class FeedbackPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, row: NewFeedbackSuggestion | FeedbackSuggestion) -> FeedbackSuggestion:
        if not isinstance(row, FeedbackSuggestion):
            row = FeedbackSuggestion(
                customer_id=row.customer_id,
                type=row.type,
                title=row.title,
                description=row.description,
                contact_email=row.contact_email,
                screenshot_key=row.screenshot_key,
                idempotency_key=row.idempotency_key,
            )
        self.session.add(row)
        await self.session.flush()
        return row

    async def obtener_por_idempotency_key(self, key: str) -> FeedbackSuggestion | None:
        stmt = select(FeedbackSuggestion).where(FeedbackSuggestion.idempotency_key == key)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def obtener_por_id(self, suggestion_id: uuid.UUID) -> FeedbackSuggestion | None:
        return await self.session.get(FeedbackSuggestion, suggestion_id)

    async def marcar_notificacion(self, suggestion_id: uuid.UUID, status: str) -> None:
        row = await self.session.get(FeedbackSuggestion, suggestion_id)
        if row is not None:
            row.notification_status = status
            row.updated_at = datetime.now(timezone.utc)
            await self.session.flush()
