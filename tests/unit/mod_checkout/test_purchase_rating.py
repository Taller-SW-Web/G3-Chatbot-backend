"""Purchase rating: ownership and one rating per order (SPEC-24 Req. 7)."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.modules.mod_checkout.application.purchase_rating_service import (
    PurchaseRatingError,
    PurchaseRatingService,
)
from src.shared.domain.module_errors import ModuleRequestError

CUSTOMER = uuid.uuid4()
ORDER = "PED-2026-0001"


class FakeRatings:
    def __init__(self):
        self.rows = {}

    async def obtener_por_order(self, order_id):
        return self.rows.get(order_id)

    async def guardar(self, row):
        saved = SimpleNamespace(
            order_id=row.order_id, customer_id=row.customer_id, rating=row.rating,
            comment=row.comment, created_at=datetime(2026, 11, 21, tzinfo=timezone.utc),
        )
        self.rows[row.order_id] = saved
        return saved


class FakeSales:
    def __init__(self, fail=None):
        self.fail = fail

    async def get_customer_order(self, order_id, holder_token):
        if self.fail:
            raise self.fail
        return {"pedidoId": order_id, "estado": "PAGADO"}


class FakeUow:
    async def commit(self):
        pass

    async def rollback(self):
        pass


def service(*, fail=None):
    return PurchaseRatingService(FakeRatings(), FakeSales(fail), FakeUow())


@pytest.mark.asyncio
async def test_rating_is_saved_for_an_owned_order():
    answer = await service().create(
        CUSTOMER, holder_token="token", order_id=ORDER, estrellas=5, comentario="Excelente"
    )
    assert answer["estrellas"] == 5 and answer["orderId"] == ORDER


@pytest.mark.asyncio
async def test_second_rating_for_the_same_order_is_409():
    svc = service()
    await svc.create(CUSTOMER, holder_token="token", order_id=ORDER, estrellas=4)
    with pytest.raises(PurchaseRatingError) as exc:
        await svc.create(CUSTOMER, holder_token="token", order_id=ORDER, estrellas=5)
    assert exc.value.code == "CONFLICTO" and exc.value.status == 409


@pytest.mark.asyncio
async def test_foreign_order_is_404():
    svc = service(fail=ModuleRequestError("sales", 403, "ACCESO_DENEGADO"))
    with pytest.raises(PurchaseRatingError) as exc:
        await svc.create(CUSTOMER, holder_token="token", order_id=ORDER, estrellas=3)
    assert exc.value.code == "RECURSO_NO_ENCONTRADO" and exc.value.status == 404
