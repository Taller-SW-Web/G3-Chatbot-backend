"""Adapter conversation (SPEC-05).

Operaciones minimas derivadas de modelo-datos.md (seccion `conversation`). No hace commit: la
transaccion la controla el caso de uso.
"""
import uuid
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, union, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.entities.conversation import Conversation as ConversationEntity
from src.shared.domain.entities.conversation import (
    ConversationSummary,
    HighlightSegment,
    SearchHighlight,
)
from src.shared.infrastructure.db.models.conversation import Conversation
from src.shared.infrastructure.db.models.message import Message

PREVIEW_MAX_CHARS = 100
# The generated tsvector columns (migration 0001) use this configuration; the query must use the same one
# or the GIN indexes cannot serve it.
SEARCH_CONFIG = "spanish"

# ts_headline marks matches with these control characters; they are stripped from the stored text first,
# so a marker in the output always comes from ts_headline and never from user content.
HIGHLIGHT_START = "\x02"
HIGHLIGHT_STOP = "\x03"
_HIGHLIGHT_MARKS = f"StartSel={HIGHLIGHT_START}, StopSel={HIGHLIGHT_STOP}"
# The title is short: mark every match and keep it whole.
TITLE_HEADLINE_OPTIONS = f"{_HIGHLIGHT_MARKS}, HighlightAll=true"
# Message fragment around the first match, sized for one sidebar line.
SNIPPET_HEADLINE_OPTIONS = f"{_HIGHLIGHT_MARKS}, MaxWords=15, MinWords=8, ShortWord=2"


def _owner_clause(owner: CallerIdentity):
    if owner.customer_id is not None:
        return Conversation.customer_id == owner.customer_id
    return (Conversation.anonymous_sid == owner.anonymous_sid) & Conversation.customer_id.is_(None)


def _without_marks(column):
    return func.translate(column, HIGHLIGHT_START + HIGHLIGHT_STOP, "")


def parse_headline(headline: str) -> tuple[HighlightSegment, ...]:
    """Split ts_headline output into plain and matching segments, dropping empty ones."""
    segments: list[HighlightSegment] = []
    for chunk in headline.split(HIGHLIGHT_START):
        match_text, sep, rest = chunk.partition(HIGHLIGHT_STOP)
        if sep:
            segments.append(HighlightSegment(match_text, True))
        else:
            rest = match_text  # no match in this chunk: it is all plain text
        if rest:
            segments.append(HighlightSegment(rest, False))
    return tuple(s for s in segments if s.text)


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

    async def search_summaries(
        self, owner: CallerIdentity, query: str, *, limit: int, offset: int
    ) -> list[ConversationSummary]:
        # `query` is a bound parameter parsed by websearch_to_tsquery, which never raises on user syntax.
        tsquery = func.websearch_to_tsquery(SEARCH_CONFIG, query)
        # Each branch is one GIN index scan; UNION de-duplicates conversations matched through several messages.
        matching_ids = union(
            select(Conversation.id).where(Conversation.title_search_vector.op("@@")(tsquery)),
            select(Message.conversation_id).where(
                Message.search_vector.op("@@")(tsquery),
                Message.role.in_(("CUSTOMER", "ASSISTANT")),  # TOOL rows hold internal payloads
            ),
        )
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
        title_headline = func.ts_headline(
            SEARCH_CONFIG, _without_marks(Conversation.title), tsquery, TITLE_HEADLINE_OPTIONS
        )
        # Fragment of the most recent matching message; NULL when only the title matched.
        snippet_headline = (
            select(func.ts_headline(
                SEARCH_CONFIG, _without_marks(Message.content), tsquery, SNIPPET_HEADLINE_OPTIONS
            ))
            .where(
                Message.conversation_id == Conversation.id,
                Message.role.in_(("CUSTOMER", "ASSISTANT")),
                Message.search_vector.op("@@")(tsquery),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
            .correlate(Conversation)
            .scalar_subquery()
        )
        stmt = (
            select(Conversation, preview, title_headline, snippet_headline)
            .where(
                _owner_clause(owner),
                Conversation.title.is_not(None),
                Conversation.id.in_(matching_ids),
            )
            .order_by(Conversation.last_message_at.desc(), Conversation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        res = await self.session.execute(stmt)
        return [
            ConversationSummary(
                id=row.id, title=row.title, preview=text or "", last_message_at=row.last_message_at,
                highlight=SearchHighlight(
                    title=parse_headline(title_hl),
                    snippet=parse_headline(snippet_hl) if snippet_hl is not None else None,
                ),
            )
            for row, text, title_hl, snippet_hl in res.all()
        ]

    async def find_owned(
        self, conversation_id: uuid.UUID, owner: CallerIdentity
    ) -> ConversationEntity | None:
        stmt = select(Conversation).where(Conversation.id == conversation_id, _owner_clause(owner))
        res = await self.session.execute(stmt)
        row = res.scalar_one_or_none()
        return _to_entity(row) if row is not None else None

    async def get_context(self, conversation_id: uuid.UUID) -> dict:
        stmt = select(Conversation.context).where(Conversation.id == conversation_id)
        context = (await self.session.execute(stmt)).scalar_one_or_none()
        return dict(context) if isinstance(context, dict) else {}

    async def save_context(self, conversation_id: uuid.UUID, context: dict) -> None:
        stmt = update(Conversation).where(Conversation.id == conversation_id).values(context=dict(context))
        await self.session.execute(stmt)

    async def get_context_for_update(self, conversation_id: uuid.UUID) -> dict:
        stmt = select(Conversation.context).where(Conversation.id == conversation_id).with_for_update()
        context = (await self.session.execute(stmt)).scalar_one_or_none()
        return dict(context) if isinstance(context, dict) else {}

    async def assign_title_if_missing(self, conversation_id: uuid.UUID, title: str) -> bool:
        stmt = (
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.title.is_(None))
            .values(title=title)
        )
        res = cast(CursorResult[Any], await self.session.execute(stmt))
        return res.rowcount == 1

    async def touch(self, conversation_id: uuid.UUID) -> None:
        stmt = (
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_message_at=func.now(), updated_at=func.now())
        )
        await self.session.execute(stmt)

    async def apply_retention(
        self,
        *,
        now: datetime,
        anonymous_ttl_days: int = 7,
        customer_archive_days: int = 90,
    ) -> tuple[int, int]:
        """Close stale anonymous conversations (>7d) and archive old customer ones (>90d)."""
        anon_cutoff = now - timedelta(days=anonymous_ttl_days)
        close_stmt = (
            update(Conversation)
            .where(
                Conversation.customer_id.is_(None),
                Conversation.status == "ACTIVE",
                Conversation.last_message_at < anon_cutoff,
            )
            .values(status="CLOSED", updated_at=now)
        )
        close_res = cast(CursorResult[Any], await self.session.execute(close_stmt))

        customer_cutoff = now - timedelta(days=customer_archive_days)
        archive_stmt = (
            update(Conversation)
            .where(
                Conversation.customer_id.is_not(None),
                Conversation.status == "ACTIVE",
                Conversation.last_message_at < customer_cutoff,
            )
            .values(status="ARCHIVED", updated_at=now)
        )
        archive_res = cast(CursorResult[Any], await self.session.execute(archive_stmt))
        return int(close_res.rowcount or 0), int(archive_res.rowcount or 0)

    async def link_anonymous_to_customer(
        self, anonymous_sid: str, customer_id: uuid.UUID
    ) -> tuple[int, str | None]:
        """Link all anonymous conversations of `anonymous_sid` to `customer_id` (SPEC-03 Req. 1 & 3).

        Also consumes and clears `context['accionPendiente']` from linked conversations so
        post-login resumes the pending action at most once.
        """
        stmt = (
            select(Conversation)
            .where(
                Conversation.anonymous_sid == anonymous_sid,
                Conversation.customer_id.is_(None),
            )
            .order_by(Conversation.last_message_at.desc(), Conversation.id.desc())
            .with_for_update()
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        if not rows:
            return 0, None
        pending_action: str | None = None
        for row in rows:
            row.customer_id = customer_id
            row.anonymous_sid = None
            ctx = dict(row.context) if isinstance(row.context, dict) else {}
            if "accionPendiente" in ctx:
                raw_action = ctx.pop("accionPendiente")
                if pending_action is None and isinstance(raw_action, str) and raw_action.strip():
                    pending_action = raw_action.strip()
                row.context = ctx
        await self.session.flush()
        return len(rows), pending_action
