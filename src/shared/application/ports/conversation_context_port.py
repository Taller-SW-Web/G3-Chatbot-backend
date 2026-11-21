"""Cross-module port: the conversation context cart and catalog need (ADR-0024).

`mod-chat` owns conversations and messages. `mod-cart` and `mod-catalog` only
need the working memory (carousel/filters), the ownership check, the activity
timestamp and the TOOL audit row, so they depend on this narrow contract
instead of importing chat's persistence adapters. `mod-chat` implements it and
registers it under the `conversation_context` capability because the manifest
DAG forbids cart/catalog from importing chat.
"""
import uuid
from typing import Any, Protocol

from src.shared.domain.caller_identity import CallerIdentity


class ConversationContextPort(Protocol):
    async def find_owned(
        self, conversation_id: uuid.UUID, owner: CallerIdentity
    ) -> Any | None:
        """The conversation when it exists and belongs to `owner`; otherwise None."""
        ...

    async def get_context(self, conversation_id: uuid.UUID) -> dict[str, Any]:
        """Working memory (carousel, filters); empty when unset or missing."""
        ...

    async def get_context_for_update(self, conversation_id: uuid.UUID) -> dict[str, Any]:
        """Lock the conversation memory until the unit of work completes."""
        ...

    async def save_context(
        self, conversation_id: uuid.UUID, context: dict[str, Any]
    ) -> None:
        """Replace the working memory. Does not commit; the caller owns the unit of work."""
        ...

    async def touch(self, conversation_id: uuid.UUID) -> None:
        """Move `last_message_at` to now (sidebar ordering)."""
        ...

    async def add_tool_audit(
        self, conversation_id: uuid.UUID, *, tool: str, code: str, text: str
    ) -> None:
        """Append the TOOL row that records a commercial action (SPEC-05)."""
        ...
