"""Public surface of `mod-chat`.

Only the composition root and tests build a conversation service from here;
no module imports chat (chat is the top of the DAG).
"""
from src.modules.mod_chat.application.dispatch_direct_action_use_case import (
    DispatchDirectActionUseCase,
)
from src.modules.mod_chat.application.interpret_and_respond_use_case import (
    InterpretAndRespondUseCase,
    InvalidMessageError,
)
from src.modules.mod_chat.application.manage_conversation_use_case import (
    ManageConversationUseCase,
)
from src.modules.mod_chat.application.tools.action_dispatcher import QUICK_ACTIONS, ActionDispatcher
from src.modules.mod_chat.module import (
    ACTION_SCOPE,
    build_interpret_and_respond,
    build_router,
    configure,
    provide_action_dispatcher,
    provide_action_scope,
    provide_chatbot_service,
    provide_interpret_and_respond,
    provide_llm_provider,
    provide_tool_executor,
    provide_tool_registry,
    provide_turn_scope,
    sanitize_sensitive_text,
)
from src.modules.mod_chat.ports.chatbot_service_port import (
    ChatbotServicePort,
    ConversationNotFoundError,
)
from src.modules.mod_chat.ports.llm_provider_port import LLMProvider
from src.modules.mod_chat.ports.tool_executor_port import ToolExecutorPort

__all__ = [
    "ACTION_SCOPE",
    "ActionDispatcher",
    "ChatbotServicePort",
    "ConversationNotFoundError",
    "DispatchDirectActionUseCase",
    "InterpretAndRespondUseCase",
    "InvalidMessageError",
    "LLMProvider",
    "ManageConversationUseCase",
    "QUICK_ACTIONS",
    "ToolExecutorPort",
    "build_interpret_and_respond",
    "build_router",
    "configure",
    "provide_action_dispatcher",
    "provide_action_scope",
    "provide_chatbot_service",
    "provide_interpret_and_respond",
    "provide_llm_provider",
    "provide_tool_executor",
    "provide_tool_registry",
    "provide_turn_scope",
    "sanitize_sensitive_text",
]
