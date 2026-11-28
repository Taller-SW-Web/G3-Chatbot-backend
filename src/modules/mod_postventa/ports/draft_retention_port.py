"""Outbound port for the local post-sale draft retention (SPEC-19/21).

The purge only deletes the chatbot's local references: unlinked evidence rows
and the expired `postventa*Draft` keys in `conversation.context`. It never
touches the file hosted by Ventas.
"""
from datetime import datetime
from typing import Protocol


class DraftRetentionPort(Protocol):
    async def purgar_borradores(self, corte: datetime) -> int:
        """Delete unlinked evidence rows created before `corte`; return the count."""
        ...

    async def purgar_borradores_contexto(self, ahora: datetime, limite: int = 1000) -> int:
        """Delete expired post-sale draft keys from `conversation.context`."""
        ...
