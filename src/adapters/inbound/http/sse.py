"""Server-Sent Events encoding of one assistant turn (SPEC-05 Req. 4). Transport only: no business rules.

Frames are `event: <name>\\ndata: <json>\\n\\n`, UTF-8:
    aceptado {"mensajeId"}   customer message persisted
    token    {"texto"}       text fragment
    bloque   {"bloque"}      structured block ready
    degradado {"motivo"}     the client discards the tokens received so far; the fallback text follows as `token`
    fin      {"mensajeId"}   assistant message id; always last on a completed turn
    error    {"code"}        unexpected failure mid-stream; the stream ends right after
"""
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Send

from src.application.dto.conversation_events import (
    BlockEvent,
    ConversationEvent,
    DegradedEvent,
    FinalEvent,
    MessageAcceptedEvent,
    TokenEvent,
)

logger = logging.getLogger(__name__)

CODE_INTERNAL = "ERROR_INTERNO"


def frame(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def encode_event(event: ConversationEvent) -> str:
    if isinstance(event, TokenEvent):
        return frame("token", {"texto": event.text})
    if isinstance(event, BlockEvent):
        return frame("bloque", {"bloque": event.block})
    if isinstance(event, FinalEvent):
        return frame("fin", {"mensajeId": str(event.message.id)})
    if isinstance(event, DegradedEvent):
        return frame("degradado", {"motivo": event.reason})
    if isinstance(event, MessageAcceptedEvent):
        return frame("aceptado", {"mensajeId": str(event.message.id)})
    raise TypeError(f"unmapped conversation event {type(event).__name__}")


async def stream_turn(stream: AsyncIterator[ConversationEvent]) -> AsyncIterator[str]:
    """Yields SSE frames. Each use-case step is shielded from cancellation: with ASGI < 2.4 Starlette cancels
    the response task on disconnect, and the use case must not be interrupted inside its own commit or
    rollback. The cancellation takes effect between events instead."""
    iterator = stream.__aiter__()
    try:
        while True:
            try:
                with anyio.CancelScope(shield=True):
                    event = await iterator.__anext__()
            except StopAsyncIteration:
                return
            except Exception:
                logger.exception("conversation turn failed mid-stream")
                yield frame("error", {"code": CODE_INTERNAL})
                return
            yield encode_event(event)
    finally:
        with anyio.CancelScope(shield=True):
            close = getattr(iterator, "aclose", None)
            try:
                if close is not None:
                    await close()
            except Exception:
                logger.exception("closing the conversation stream failed")


class TurnStreamingResponse(StreamingResponse):
    """SSE response that owns the turn's resources (database session): they are released when the stream
    ends, fails or the client leaves. A `yield` dependency cannot do this, its cleanup may run before the
    body is streamed. With ASGI >= 2.4 a disconnect only surfaces as an error from `send`, so the frame
    generator is closed explicitly here instead of waiting for garbage collection."""

    def __init__(self, stream: AsyncIterator[ConversationEvent], resources: AsyncExitStack):
        frames = stream_turn(stream)
        super().__init__(
            frames,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self._frames = frames
        self._resources = resources

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self._frames.aclose()
                finally:
                    await self._resources.aclose()
