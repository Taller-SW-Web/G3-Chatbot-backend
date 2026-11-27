"""Evidence drafts uploaded in a request are linked to the created return (SPEC-21)."""
import uuid
from dataclasses import dataclass, field
from typing import Any

import anyio

from src.modules.mod_postventa.adapters.fake_postventa import FakePostventaPort
from src.modules.mod_postventa.application.postventa_service import PostventaService
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort

CUSTOMER = uuid.UUID("11111111-1111-1111-1111-111111111111")
HOLDER = f"fake-access-{CUSTOMER}"


class _Conversations:
    def __init__(self, owned: dict[uuid.UUID, uuid.UUID]):
        self.owned = owned

    async def find_owned(self, conversation_id, owner):
        return object() if self.owned.get(conversation_id) == owner.customer_id else None


@dataclass
class _EvidenceRow:
    id: uuid.UUID
    conversation_id: uuid.UUID
    url: str
    return_id: str | None = None


@dataclass
class _EvidenceStore:
    owners: dict[uuid.UUID, uuid.UUID]
    rows: list[_EvidenceRow] = field(default_factory=list)

    async def guardar(self, reference):
        row = _EvidenceRow(
            id=uuid.uuid4(),
            conversation_id=reference.conversation_id,
            url=reference.url,
        )
        self.rows.append(row)
        return row

    async def listar_borrador_por_urls(self, urls, customer_id):
        values = set(urls)
        return [
            row
            for row in self.rows
            if row.return_id is None
            and row.url in values
            and self.owners.get(row.conversation_id) == customer_id
        ]

    async def asociar_devolucion(self, evidence_id, conversation_id, return_id):
        for row in self.rows:
            if row.id == evidence_id and row.conversation_id == conversation_id:
                if row.return_id in (None, return_id):
                    row.return_id = return_id
                    return row
                return None
        return None


class _MemoryRefs:
    def __init__(self):
        self.rows: dict[str, Any] = {}

    async def guardar(self, row):
        self.rows[row.idempotency_key] = row
        return row

    async def obtener_por_idempotency_key(self, key):
        return self.rows.get(key)


def test_two_uploaded_evidences_keep_the_return_id():
    conversation_id = uuid.uuid4()
    store = _EvidenceStore({conversation_id: CUSTOMER})
    port = FakePostventaPort()
    service = PostventaService(
        postventa=port,
        sales=FakeSalesPort(),
        return_refs=_MemoryRefs(),
        evidence=store,
        conversations=_Conversations(store.owners),
    )

    async def run():
        first = await service.upload_evidence(
            holder_token=HOLDER,
            file_name="foto1.png",
            content_type="image/png",
            content=b"png-1",
            conversation_id=conversation_id,
            customer_id=CUSTOMER,
        )
        second = await service.upload_evidence(
            holder_token=HOLDER,
            file_name="foto2.png",
            content_type="image/png",
            content=b"png-2",
            conversation_id=conversation_id,
            customer_id=CUSTOMER,
        )
        detail = await service.create_return(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            payload={
                "pedidoId": "PED-2026-00001",
                "tipo": "CAMBIO",
                "motivo": "PRODUCTO_DEFECTUOSO",
                "items": [{"productoId": "P-1", "cantidad": 1}],
                "evidencias": [
                    {"tipo": first["tipo"], "url": first["url"]},
                    {"tipo": second["tipo"], "url": second["url"]},
                ],
            },
            idempotency_key="return-evidence-link",
        )
        return detail

    detail = anyio.run(run)

    assert len(store.rows) == 2
    assert {row.return_id for row in store.rows} == {detail["devolucionId"]}
    assert store.rows[0].return_id == store.rows[1].return_id
