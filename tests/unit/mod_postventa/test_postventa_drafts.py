"""Post-sale drafts in `conversation.context`: reload and 24 h expiry (SPEC-19/21)."""
import copy
import uuid
from datetime import datetime, timedelta, timezone

import anyio
import pytest

from src.modules.mod_postventa.adapters.fake_postventa import FakePostventaPort
from src.modules.mod_postventa.application.postventa_dto import PostventaError
from src.modules.mod_postventa.application.postventa_service import (
    CLAIM_DRAFT_CONTEXT_KEY,
    DRAFT_TTL,
    RETURN_DRAFT_CONTEXT_KEY,
    PostventaService,
)
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort

CUSTOMER = uuid.UUID("11111111-1111-1111-1111-111111111111")
HOLDER = f"fake-access-{CUSTOMER}"


class _Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _Conversations:
    def __init__(self, owned: dict[uuid.UUID, uuid.UUID]):
        self.owned = owned
        self.contexts: dict[uuid.UUID, dict] = {}

    async def find_owned(self, conversation_id, owner):
        return object() if self.owned.get(conversation_id) == owner.customer_id else None

    async def get_context(self, conversation_id):
        return copy.deepcopy(self.contexts.get(conversation_id, {}))

    async def get_context_for_update(self, conversation_id):
        return copy.deepcopy(self.contexts.get(conversation_id, {}))

    async def save_context(self, conversation_id, context):
        self.contexts[conversation_id] = copy.deepcopy(context)


class _MemoryRefs:
    def __init__(self):
        self.rows = {}

    async def guardar(self, row):
        self.rows[row.idempotency_key] = row
        return row

    async def obtener_por_idempotency_key(self, key):
        return self.rows.get(key)


def _service(conversations, clock, port=None):
    return PostventaService(
        postventa=port or FakePostventaPort(),
        sales=FakeSalesPort(),
        return_refs=_MemoryRefs(),
        claim_refs=_MemoryRefs(),
        conversations=conversations,
        clock=clock,
    )


def test_return_draft_survives_reload_and_expires_after_24_hours():
    conversation_id = uuid.uuid4()
    conversations = _Conversations({conversation_id: CUSTOMER})
    clock = _Clock(datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc))
    payload = {
        "pedidoId": "PED-2026-00001",
        "tipo": "CAMBIO",
        "motivo": "TALLA_INCORRECTA",
        "items": [{"productoId": "P-1", "cantidad": 1}],
        "evidencias": [{"tipo": "IMAGEN", "url": "https://ventas.example/ev-1.jpg"}],
    }

    anyio.run(
        lambda: _service(conversations, clock).save_return_draft(
            conversation_id=conversation_id,
            customer_id=CUSTOMER,
            payload=payload,
        )
    )

    reloaded = _service(conversations, clock)
    assert anyio.run(
        lambda: reloaded.get_return_draft(
            conversation_id=conversation_id, customer_id=CUSTOMER
        )
    ) == payload
    assert RETURN_DRAFT_CONTEXT_KEY in conversations.contexts[conversation_id]

    clock.value += timedelta(hours=23, minutes=59)
    assert anyio.run(
        lambda: reloaded.get_return_draft(
            conversation_id=conversation_id, customer_id=CUSTOMER
        )
    ) == payload

    clock.value += timedelta(minutes=1, seconds=1)
    assert anyio.run(
        lambda: reloaded.get_return_draft(
            conversation_id=conversation_id, customer_id=CUSTOMER
        )
    ) is None
    assert RETURN_DRAFT_CONTEXT_KEY not in conversations.contexts[conversation_id]


def test_claim_draft_survives_reload_and_expires_after_24_hours():
    conversation_id = uuid.uuid4()
    conversations = _Conversations({conversation_id: CUSTOMER})
    clock = _Clock(datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc))
    payload = {
        "pedidoId": "PED-2026-00002",
        "tipo": "RECLAMO",
        "motivo": "INCUMPLIMIENTO_PLAZO_ENTREGA",
        "detalle": "El pedido no llegó en la fecha prometida.",
    }

    anyio.run(
        lambda: _service(conversations, clock).save_claim_draft(
            conversation_id=conversation_id,
            customer_id=CUSTOMER,
            payload=payload,
        )
    )

    reloaded = _service(conversations, clock)
    assert anyio.run(
        lambda: reloaded.get_claim_draft(
            conversation_id=conversation_id, customer_id=CUSTOMER
        )
    ) == payload
    assert CLAIM_DRAFT_CONTEXT_KEY in conversations.contexts[conversation_id]

    clock.value += DRAFT_TTL + timedelta(seconds=1)
    assert anyio.run(
        lambda: reloaded.get_claim_draft(
            conversation_id=conversation_id, customer_id=CUSTOMER
        )
    ) is None
    assert CLAIM_DRAFT_CONTEXT_KEY not in conversations.contexts[conversation_id]


def test_draft_of_another_customer_is_not_readable():
    conversation_id = uuid.uuid4()
    conversations = _Conversations({conversation_id: uuid.uuid4()})
    clock = _Clock(datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc))

    with pytest.raises(PostventaError) as exc:
        anyio.run(
            lambda: _service(conversations, clock).get_return_draft(
                conversation_id=conversation_id, customer_id=CUSTOMER
            )
        )
    assert exc.value.code == "RECURSO_NO_ENCONTRADO"


class _FailingCreatePort(FakePostventaPort):
    async def create_return(self, *args, **kwargs):
        raise TimeoutError("Ventas no responde")


def test_failed_registration_keeps_the_return_draft_and_success_clears_it():
    conversation_id = uuid.uuid4()
    conversations = _Conversations({conversation_id: CUSTOMER})
    clock = _Clock(datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc))
    payload = {
        "pedidoId": "PED-2026-00003",
        "tipo": "CAMBIO",
        "motivo": "TALLA_INCORRECTA",
        "items": [{"productoId": "P-1", "cantidad": 1}],
        "evidencias": [],
    }

    failing = _service(conversations, clock, port=_FailingCreatePort())
    with pytest.raises(TimeoutError):
        anyio.run(
            lambda: failing.create_return(
                customer_id=CUSTOMER,
                holder_token=HOLDER,
                payload=payload,
                idempotency_key="return-draft-fail",
                conversation_id=conversation_id,
            )
        )

    reloaded = _service(conversations, clock)
    assert anyio.run(
        lambda: reloaded.get_return_draft(
            conversation_id=conversation_id, customer_id=CUSTOMER
        )
    ) == payload

    anyio.run(
        lambda: reloaded.create_return(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            payload=payload,
            idempotency_key="return-draft-ok",
            conversation_id=conversation_id,
        )
    )
    assert anyio.run(
        lambda: reloaded.get_return_draft(
            conversation_id=conversation_id, customer_id=CUSTOMER
        )
    ) is None
