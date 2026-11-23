"""SAVEPOINT behavior of the post-sale reference adapters (no PostgreSQL needed)."""
import uuid

import anyio
from sqlalchemy.exc import IntegrityError

from src.modules.mod_postventa.adapters.claim_ref_postgres_adapter import ClaimRefPostgresAdapter
from src.modules.mod_postventa.adapters.return_ref_postgres_adapter import ReturnRefPostgresAdapter
from src.modules.mod_postventa.domain.references import NewClaimReference, NewReturnReference
from src.shared.infrastructure.db.models.claim_ref import ClaimRef
from src.shared.infrastructure.db.models.return_ref import ReturnRef

CUSTOMER = uuid.UUID("11111111-1111-1111-1111-111111111111")


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeSession:
    def __init__(self, existing):
        self.existing = existing
        self.nested = 0
        self.rollbacks = 0
        self.added = []

    def begin_nested(self):
        self.nested += 1
        return _Nested()

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        raise IntegrityError("insert", {}, Exception("duplicate idempotency_key"))

    async def rollback(self):
        self.rollbacks += 1

    async def execute(self, _statement):
        return _Result(self.existing)


def test_claim_ref_duplicate_uses_savepoint_without_full_rollback() -> None:
    existing = ClaimRef(
        claim_id="REC-2026-0015",
        code="REC-2026-0015",
        order_id="PED-2026-90101",
        customer_id=CUSTOMER,
        idempotency_key="claim-key",
    )
    session = _FakeSession(existing)
    adapter = ClaimRefPostgresAdapter(session)

    result = anyio.run(
        lambda: adapter.guardar(
            NewClaimReference(
                claim_id="REC-2026-0099",
                code="REC-2026-0099",
                order_id="PED-2026-90101",
                customer_id=CUSTOMER,
                idempotency_key="claim-key",
            )
        )
    )

    assert result is existing
    assert session.nested == 1
    assert session.rollbacks == 0
    assert len(session.added) == 1


def test_return_ref_duplicate_uses_savepoint_without_full_rollback() -> None:
    existing = ReturnRef(
        return_id="DEV-2026-0028",
        code="DEV-2026-0028",
        order_id="PED-2026-90201",
        customer_id=CUSTOMER,
        requested_type="MONEY_REFUND",
        idempotency_key="return-key",
    )
    session = _FakeSession(existing)
    adapter = ReturnRefPostgresAdapter(session)

    result = anyio.run(
        lambda: adapter.guardar(
            NewReturnReference(
                return_id="DEV-2026-0099",
                code="DEV-2026-0099",
                order_id="PED-2026-90201",
                customer_id=CUSTOMER,
                requested_type="DEVOLUCION_DINERO",
                idempotency_key="return-key",
            )
        )
    )

    assert result is existing
    assert session.nested == 1
    assert session.rollbacks == 0
    assert session.added[0].requested_type == "MONEY_REFUND"
