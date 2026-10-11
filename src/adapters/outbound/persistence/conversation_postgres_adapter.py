"""Adapter conversation (H2-07, SPEC-05).

Operaciones minimas derivadas de modelo-datos.md (seccion `conversation`). No hace commit: la
transaccion la controla el caso de uso.
"""
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.conversation import Conversation
from src.adapters.outbound.persistence.models.message import Message
from src.domain.entities.conversation import Conversation as ConversationEntity
from src.domain.entities.conversation import ConversationSummary
from src.domain.value_objects.caller_identity import CallerIdentity

PREVIEW_MAX_CHARS = 100


def _owner_clause(owner: CallerIdentity):
    if owner.customer_id is not None:
        return Conversation.customer_id == owner.customer_id
    return (Conversation.anonymous_sid == owner.anonymous_sid) & Conversation.customer_id.is_(None)


def _to_entity(row: Conversation) -> ConversationEntity:
    return ConversationEntity(
        id=row.id, title=row.title, last_message_at=row.last_message_at, summary=row.summary
    )


class ConversationPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, row: Conversation) -> Conversation:
        self.session.add(row)
        await self.session.flush()
        return row

    async def obtener_por_id(self, id: uuid.UUID) -> Conversation | None:
        stmt = select(Conversation).where(Conversation.id == id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def listar_por_cliente(self, customer_id: uuid.UUID) -> list[Conversation]:
        # Solo conversaciones con title: las vacias no aparecen en la barra lateral.
        stmt = (
            select(Conversation)
            .where(Conversation.customer_id == customer_id, Conversation.title.is_not(None))
            .order_by(Conversation.last_message_at.desc())
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def obtener_anonima_por_sid(self, anonymous_sid: str) -> Conversation | None:
        stmt = (
            select(Conversation)
            .where(Conversation.anonymous_sid == anonymous_sid, Conversation.customer_id.is_(None))
            .order_by(Conversation.last_message_at.desc())
            .limit(1)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    # Methods below implement ConversationRepositoryPort (English names, domain objects).

    async def create(self, owner: CallerIdentity) -> ConversationEntity:
        row = await self.guardar(
            Conversation(customer_id=owner.customer_id, anonymous_sid=owner.anonymous_sid)
        )
        await self.session.refresh(row)  # load server defaults (last_message_at)
        return _to_entity(row)

    async def list_summaries(
        self, owner: CallerIdentity, *, limit: int, offset: int
    ) -> list[ConversationSummary]:
        # One query: the preview is a correlated subquery over the latest CUSTOMER/ASSISTANT message.
        preview = (
            select(func.left(Message.content, PREVIEW_MAX_CHARS))
            .where(
                Message.conversation_id == Conversation.id,
                Message.role.in_(("CUSTOMER", "ASSISTANT")),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
            .correlate(Conversation)
            .scalar_subquery()
        )
        stmt = (
            select(Conversation, preview)
            .where(_owner_clause(owner), Conversation.title.is_not(None))
            .order_by(Conversation.last_message_at.desc(), Conversation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        res = await self.session.execute(stmt)
        return [
            ConversationSummary(
                id=row.id, title=row.title, preview=text or "", last_message_at=row.last_message_at
            )
            for row, text in res.all()
        ]

    async def find_owned(
        self, conversation_id: uuid.UUID, owner: CallerIdentity
    ) -> ConversationEntity | None:
        stmt = select(Conversation).where(Conversation.id == conversation_id, _owner_clause(owner))
        res = await self.session.execute(stmt)
        row = res.scalar_one_or_none()
        return _to_entity(row) if row is not None else None

    async def assign_title_if_missing(self, conversation_id: uuid.UUID, title: str) -> bool:
        stmt = (
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.title.is_(None))
            .values(title=title)
        )
        res = await self.session.execute(stmt)
        return res.rowcount == 1

    async def touch(self, conversation_id: uuid.UUID) -> None:
        stmt = (
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_message_at=func.now(), updated_at=func.now())
        )
        await self.session.execute(stmt)
