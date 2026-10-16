"""Dispatch UI actions through the same ToolRegistry handlers as LLM calls (SPEC-05 Req. 8)."""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.application.tools.action_dispatcher import ActionDispatcher
from src.application.tools.tool_registry import ExecutionContext
from src.domain.entities.message import Message, MessageRole
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.inbound.chatbot_service_port import ConversationNotFoundError
from src.ports.outbound.conversation_repository_port import ConversationRepositoryPort
from src.ports.outbound.message_repository_port import MessageRepositoryPort
from src.ports.outbound.unit_of_work_port import UnitOfWorkPort


@dataclass(frozen=True, slots=True)
class DirectActionResult:
    code: str
    message: Message
    text: str
    blocks: tuple[dict[str, Any], ...]
    data: dict[str, Any]


class DispatchDirectActionUseCase:
    def __init__(
        self,
        conversations: ConversationRepositoryPort,
        messages: MessageRepositoryPort,
        uow: UnitOfWorkPort,
        dispatcher: ActionDispatcher,
        *,
        sanitize_text=lambda text: text,
    ) -> None:
        self._conversations = conversations
        self._messages = messages
        self._uow = uow
        self._dispatcher = dispatcher
        self._sanitize = sanitize_text

    async def execute(
        self,
        caller: CallerIdentity | None,
        conversation_id: uuid.UUID,
        action: Mapping[str, Any],
    ) -> DirectActionResult:
        if caller is None:
            raise ConversationNotFoundError()
        conversation = await self._conversations.find_owned(conversation_id, caller)
        if conversation is None:
            raise ConversationNotFoundError()

        context = ExecutionContext(
            conversacion_id=str(conversation_id),
            cliente_id=str(caller.customer_id) if caller.customer_id is not None else None,
            chat_sid=caller.anonymous_sid,
        )
        try:
            result = await self._dispatcher.despachar(action, context)
            raw_data = dict(result.data)
            raw_blocks = raw_data.pop("bloques", ())
            blocks = tuple(
                json.loads(self._sanitize(json.dumps(dict(block), ensure_ascii=False, default=str)))
                for block in raw_blocks if isinstance(block, Mapping)
            ) if isinstance(raw_blocks, list) else ()
            if result.code != "OK" and not blocks:
                blocks = ({
                    "id": str(uuid.uuid4()),
                    "tipo": "ERROR",
                    "datos": {"code": result.code},
                },)
            text = self._sanitize(str(raw_data.pop("texto", "")))
            safe_data = json.loads(self._sanitize(json.dumps(raw_data, ensure_ascii=False, default=str)))

            action_type = str(action.get("tipo", "accion"))
            audit_text = self._sanitize(json.dumps(
                {"tipo": action_type, "payload": action.get("payload", {})},
                ensure_ascii=False,
                default=str,
            ))
            await self._messages.add(
                conversation_id,
                MessageRole.TOOL,
                audit_text,
                tool_name=action_type,
                result_code=result.code,
            )
            assistant_message = await self._messages.add(
                conversation_id,
                MessageRole.ASSISTANT,
                text,
                blocks={"blocks": list(blocks)} if blocks else None,
            )
            await self._conversations.touch(conversation_id)
            await self._uow.commit()
            return DirectActionResult(result.code, assistant_message, text, blocks, safe_data)
        except BaseException:
            await self._uow.rollback()
            raise
