"""In-process harness for the H5-E1 reference conversations.

The real LLM talks to the production agent loop, while every downstream module
is fake: catalog and cart use cases run over the deterministic seed data and
in-memory repositories. `RecordingToolExecutor` keeps the exact tool calls and
results so the eval can assert on tools, arguments and data instead of text.

The eval cases must never invent prices or stock: the fake data comes from the
same `seeds/` that `MODULES_MODE=fake` uses (SPEC-06, SPEC-09, SPEC-11).
"""
from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.modules.mod_cart.application.manage_cart_use_case import ManageCartUseCase
from src.modules.mod_catalog.adapters.fake_products import FakeCatalogPort, FakeInventoryPort
from src.modules.mod_catalog.application.manage_catalog_use_case import ManageCatalogUseCase
from src.modules.mod_catalog.application.tools import build_tool_registry
from src.modules.mod_chat.adapters.registry_tool_adapter import RegistryToolAdapter
from src.modules.mod_chat.application.dto.conversation_events import (
    BlockEvent,
    ConversationEvent,
    FinalEvent,
)
from src.modules.mod_chat.application.interpret_and_respond_use_case import (
    InterpretAndRespondUseCase,
)
from src.modules.mod_chat.application.tools.cart_handlers import build_cart_handlers
from src.modules.mod_chat.application.tools.catalog_handlers import build_catalog_handlers
from src.modules.mod_chat.domain.output_validator import extract_pen_amounts
from src.modules.mod_chat.ports.llm_provider_port import (
    LLMEvent,
    LLMMessage,
    LLMProvider,
    ToolDefinition,
)
from src.modules.mod_chat.ports.tool_executor_port import (
    ToolExecutionContext,
    ToolExecutorPort,
    ToolResult,
)
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.entities.conversation import Conversation
from src.shared.domain.entities.message import Message, MessageRole
from src.shared.domain.sensitive_data_filter import SensitiveDataFilter
from tests.fakes.fake_commerce import FakeCarts, FakeItems, FakeUow

NOW = datetime.now(timezone.utc)
_SENSITIVE_FILTER = SensitiveDataFilter()

# Same evidence the agent loop accepts from a successful tool result so the
# eval can check that no reply introduces an amount outside the tools.
_MONEY_EVIDENCE_KEYS = frozenset({
    "amount", "coupon_discount", "descuento", "descuento_monto", "discount_amount",
    "line_total", "list_price", "monto", "offer_price", "price", "precio", "precio_lista",
    "precio_oferta", "precio_regular", "precio_total", "precio_unitario", "regular_price",
    "shipping_cost", "subtotal", "total", "unit_price",
})
_UNIT_PRICE_KEYS = frozenset({"precio", "price", "unit_price", "precio_unitario"})
_QUANTITY_KEYS = frozenset({"cantidad", "quantity"})


def _snake_case(value: Any) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", str(value)).casefold()


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


class FakeConversations:
    """One owned conversation; the commercial use cases share its working memory."""

    def __init__(self, owner: CallerIdentity) -> None:
        self.id = uuid.uuid4()
        self.owner = owner
        self.title: str | None = None
        self.summary: str | None = None
        self.context: dict[str, Any] = {}
        self.touches = 0

    async def get_context(self, conversation_id: uuid.UUID) -> dict[str, Any]:
        return dict(self.context)

    async def save_context(self, conversation_id: uuid.UUID, context: Mapping[str, Any]) -> None:
        self.context = dict(context)

    async def get_context_for_update(self, conversation_id: uuid.UUID) -> dict[str, Any]:
        return dict(self.context)

    async def find_owned(self, conversation_id: uuid.UUID, owner: CallerIdentity) -> Conversation | None:
        if conversation_id == self.id and owner == self.owner:
            return Conversation(id=self.id, title=self.title, last_message_at=NOW, summary=self.summary)
        return None

    async def assign_title_if_missing(self, conversation_id: uuid.UUID, title: str) -> bool:
        if self.title is not None:
            return False
        self.title = title
        return True

    async def touch(self, conversation_id: uuid.UUID) -> None:
        self.touches += 1


class FakeMessages:
    """Keeps every row; `list_dialogue` hides tool rows and honours the limit, like the adapter."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def add(self, conversation_id: uuid.UUID, role: MessageRole, text: str, **extra: Any) -> Message:
        row = {"id": uuid.uuid4(), "role": role, "text": text, **extra}
        self.rows.append(row)
        return Message(
            id=row["id"], conversation_id=conversation_id, role=role, text=text,
            blocks=extra.get("blocks"), created_at=NOW,
        )

    async def list_dialogue(self, conversation_id: uuid.UUID, *, limit: int) -> list[Message]:
        rows = [row for row in self.rows if row["role"] is not MessageRole.TOOL]
        return [
            Message(
                id=row["id"], conversation_id=conversation_id, role=row["role"], text=row["text"],
                blocks=row.get("blocks"), created_at=NOW,
            )
            for row in rows[-limit:]
        ]

    def by_role(self, role: MessageRole) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["role"] is role]


class FakeConversationContext:
    """Narrow `ConversationContextPort` over the chat conversation fake."""

    def __init__(self, conversations: FakeConversations) -> None:
        self._conversations = conversations

    async def find_owned(self, conversation_id: uuid.UUID, caller: CallerIdentity) -> Conversation | None:
        return await self._conversations.find_owned(conversation_id, caller)

    async def get_context(self, conversation_id: uuid.UUID) -> dict[str, Any]:
        return await self._conversations.get_context(conversation_id)

    async def get_context_for_update(self, conversation_id: uuid.UUID) -> dict[str, Any]:
        return await self._conversations.get_context_for_update(conversation_id)

    async def save_context(self, conversation_id: uuid.UUID, context: Mapping[str, Any]) -> None:
        await self._conversations.save_context(conversation_id, context)

    async def touch(self, conversation_id: uuid.UUID) -> None:
        await self._conversations.touch(conversation_id)


class RecordingLLMProvider:
    """Delegates to a real or fake provider and records what the model received."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.calls: list[tuple[list[LLMMessage], list[ToolDefinition]]] = []

    @property
    def supports_vision(self) -> bool:
        return bool(getattr(self.inner, "supports_vision", False))

    async def stream(
        self, messages: Sequence[LLMMessage], tools: Sequence[ToolDefinition]
    ) -> AsyncIterator[LLMEvent]:
        self.calls.append((list(messages), list(tools)))
        async for event in self.inner.stream(messages, tools):
            yield event


@dataclass(frozen=True)
class ToolCallRecord:
    """One request the model made and the server-side result it produced."""

    name: str
    arguments: Mapping[str, Any]
    result: ToolResult


class RecordingToolExecutor:
    """Transparent proxy over the production registry adapter."""

    def __init__(self, inner: ToolExecutorPort) -> None:
        self._inner = inner
        self.calls: list[ToolCallRecord] = []

    def definitions(self) -> list[ToolDefinition]:
        return self._inner.definitions()

    async def execute(
        self, name: str, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        result = await self._inner.execute(name, arguments, context)
        self.calls.append(ToolCallRecord(name=name, arguments=dict(arguments), result=result))
        return result

    def named(self, name: str) -> list[ToolCallRecord]:
        return [call for call in self.calls if call.name == name]

    @property
    def names(self) -> list[str]:
        return [call.name for call in self.calls]


class ShoppingWorld:
    """Production agent loop + real tool registry over in-process fake modules."""

    def __init__(
        self,
        llm: LLMProvider,
        *,
        tools: ToolExecutorPort | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.caller = CallerIdentity(customer_id=uuid.uuid4())
        self.conversations = FakeConversations(self.caller)
        self.messages = FakeMessages()
        self.uow = FakeUow()
        self.llm = RecordingLLMProvider(llm)
        self.catalog = FakeCatalogPort()
        self.inventory = FakeInventoryPort()
        self.context = FakeConversationContext(self.conversations)
        self.catalog_use_case = ManageCatalogUseCase(
            self.catalog, self.inventory, self.context, self.uow
        )
        self.carts = FakeCarts()
        self.items = FakeItems()
        self.cart_use_case = ManageCartUseCase(
            self.carts, self.items, self.catalog, self.inventory, self.context, self.uow
        )
        handlers = {
            **build_catalog_handlers(self.catalog_use_case),
            **build_cart_handlers(self.cart_use_case),
        }
        self.registry = build_tool_registry(handlers)
        self.tools = RecordingToolExecutor(tools or RegistryToolAdapter(self.registry))
        self.use_case = InterpretAndRespondUseCase(
            self.conversations,
            self.messages,
            self.uow,
            self.llm,
            self.tools,
            assistant_name="Botleta",
            llm_timeout_seconds=timeout_seconds,
            sanitize_text=lambda text: _SENSITIVE_FILTER.redactar(text).texto,
        )

    @property
    def conversation_id(self) -> uuid.UUID:
        return self.conversations.id

    async def seed_cart(self) -> None:
        """One known line, two units, from the deterministic seed catalog."""
        cart = await self.carts.crear_para_cliente(self.caller.customer_id)
        await self.items.agregar(
            cart_id=cart.id,
            product_id="prod-001",
            sku="NIK-REV7-42-NEG",
            name="Nike Revolution 7",
            variant_description="Talla 42 · Negro",
            quantity=2,
            unit_price_ref=279.90,
        )

    async def run(self, text: str) -> list[ConversationEvent]:
        stream = await self.use_case.send_message(self.caller, self.conversations.id, text)
        return [event async for event in stream]


@dataclass
class EvalCase:
    """Report of one reference conversation; assertions are recorded as they run."""

    name: str
    scenario: str
    status: str = "pending"
    duration_ms: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    llm_calls: int = 0
    skip_reason: str | None = None
    failure: str | None = None

    def check(self, name: str, condition: Any, *, detail: Any = None) -> None:
        passed = bool(condition)
        self.assertions.append({"name": name, "passed": passed, "detail": _jsonable(detail)})
        if not passed:
            message = f"{name}: {detail}" if detail is not None else name
            raise AssertionError(message)

    def record_tool_calls(self, calls: Sequence[ToolCallRecord]) -> None:
        self.tool_calls = [
            {
                "name": call.name,
                "arguments": _jsonable(call.arguments),
                "code": call.result.code,
                "is_error": call.result.is_error,
                "blocks": [str(block.get("tipo", "")) for block in call.result.blocks],
            }
            for call in calls
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scenario": self.scenario,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "tool_calls": self.tool_calls,
            "assertions": self.assertions,
            "final_text": self.final_text,
            "llm_calls": self.llm_calls,
            "skip_reason": self.skip_reason,
            "failure": self.failure,
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(child) for child in value), key=str)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


async def run_reference_turn(case: EvalCase, world: ShoppingWorld, text: str) -> list[ConversationEvent]:
    """Send one customer message and copy tools/text/LLM evidence into the report."""
    try:
        events = await world.run(text)
    except BaseException:
        case.record_tool_calls(world.tools.calls)
        case.llm_calls = len(world.llm.calls)
        raise
    case.final_text = final_text(events)
    case.record_tool_calls(world.tools.calls)
    case.llm_calls = len(world.llm.calls)
    return events


def final_text(events: Sequence[ConversationEvent]) -> str:
    finals = [event for event in events if isinstance(event, FinalEvent)]
    return finals[-1].message.text if finals else ""


def blocks(events: Sequence[ConversationEvent]) -> list[dict[str, Any]]:
    return [event.block for event in events if isinstance(event, BlockEvent)]


def block_types(events: Sequence[ConversationEvent]) -> list[str]:
    return [str(block.get("tipo", "")) for block in blocks(events)]


def _collect_amounts(value: Any, amounts: set[Decimal], currency: str = "PEN") -> None:
    if isinstance(value, Mapping):
        current_currency = str(value.get("currency", currency or "PEN")).upper()
        quantity = next(
            (value[key] for key in value if _snake_case(key) in _QUANTITY_KEYS), None
        )
        if isinstance(quantity, int) and not isinstance(quantity, bool) and quantity >= 1:
            for key, child in value.items():
                if _snake_case(key) in _UNIT_PRICE_KEYS:
                    unit = _as_decimal(child)
                    if unit is not None:
                        amounts.add(unit * quantity)
        for key, child in value.items():
            normalized_key = _snake_case(key)
            if normalized_key == "texto" and isinstance(child, str):
                amounts.update(extract_pen_amounts(child))
            elif normalized_key in _MONEY_EVIDENCE_KEYS and current_currency in {"PEN", "S/"}:
                number = _as_decimal(child)
                if number is not None:
                    amounts.add(number)
            _collect_amounts(child, amounts, current_currency)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _collect_amounts(child, amounts, currency)


def authorized_amounts(calls: Sequence[ToolCallRecord]) -> set[Decimal]:
    """Amounts backed by successful tool data or blocks, like the agent loop authorizes."""
    amounts: set[Decimal] = set()
    for call in calls:
        if call.result.code != "OK":
            continue
        _collect_amounts(call.result.data, amounts)
        _collect_amounts(list(call.result.blocks), amounts)
    return amounts


def assert_no_invented_amounts(case: EvalCase, world: ShoppingWorld, text: str) -> None:
    mentioned = extract_pen_amounts(text)
    allowed = authorized_amounts(world.tools.calls)
    case.check(
        "sin importes fuera de las herramientas",
        mentioned <= allowed,
        detail={
            "mentioned": sorted(float(amount) for amount in mentioned),
            "allowed": sorted(float(amount) for amount in allowed),
        },
    )
    case.check(
        "el validador no reemplazó importes",
        "[importe no verificado]" not in text,
        detail=text,
    )


def assert_no_amounts(case: EvalCase, text: str) -> None:
    mentioned = extract_pen_amounts(text)
    case.check(
        "no introdujo importes",
        mentioned == set(),
        detail=sorted(float(amount) for amount in mentioned),
    )
