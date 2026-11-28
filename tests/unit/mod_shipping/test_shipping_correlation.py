"""`POST /envio/cotizar` correlation (#248).

The BFF must forward the request's `X-Correlation-Id` to G2 instead of
inventing a new one, so Despacho's logs join the original BFF request.
"""
import uuid

import httpx
import pytest
import respx

from src.infrastructure.main import create_app
from src.modules.mod_shipping.adapters.despacho_client import DespachoClient
from src.modules.mod_shipping.application.manage_shipping_use_case import (
    ManageShippingUseCase,
)
from src.modules.mod_shipping.module import provide_shipping_service
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.caller_identity import (
    get_caller_identity,
)
from tests.fakes.fake_commerce import FakeCarts, FakeItems

BASE_URL = "http://mock-despacho:4013"
CUSTOMER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
SKU = "NIK-REV7-42-NEG"
QUOTE = {
    "coberturaDisponible": True,
    "costoEnvio": 12.5,
    "moneda": "PEN",
    "plazoEstimadoDiasHabiles": 1,
    "fechaEstimadaEntrega": "2026-11-07",
    "nombreZona": "Lima Metropolitana",
}
PAYLOAD = {
    "destinatario": "Ana Díaz",
    "direccion": "Av. Larco 1234, dpto. 502",
    "distrito": "Miraflores",
    "referencia": "Frente al parque",
}


class _Tokens:
    def __init__(self):
        self.correlation_ids: list[str | None] = []

    async def get_token(
        self, *, correlation_id=None, scope=None, audience=None
    ) -> str:
        self.correlation_ids.append(correlation_id)
        return "svc-token"


class _Security:
    async def list_addresses(self, user_id, holder_token, *, correlation_id=None):
        return []


async def _app_with_real_dispatch(tokens):
    carts, items = FakeCarts(), FakeItems()
    cart = await carts.crear_para_cliente(CUSTOMER_ID)
    await items.agregar(
        cart_id=cart.id,
        product_id="prod-1",
        sku=SKU,
        name="Zapatilla Runner",
        quantity=1,
        unit_price_ref=279.9,
    )
    use_case = ManageShippingUseCase(
        carts,
        items,
        DespachoClient(
            base_url=BASE_URL,
            service_token_provider=tokens,
            timeout_seconds=4.0,
        ),
        _Security(),
    )
    app = create_app()
    app.dependency_overrides[provide_shipping_service] = lambda: use_case
    app.dependency_overrides[get_caller_identity] = lambda: CallerIdentity(
        customer_id=CUSTOMER_ID
    )
    return app


@respx.mock
@pytest.mark.asyncio
async def test_quote_forwards_the_incoming_correlation_id_to_despacho():
    route = respx.post(f"{BASE_URL}/cotizaciones").mock(
        return_value=httpx.Response(200, json=QUOTE)
    )
    incoming = str(uuid.uuid4())
    tokens = _Tokens()
    app = await _app_with_real_dispatch(tokens)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/envio/cotizar",
            headers={"X-Correlation-Id": incoming},
            json=PAYLOAD,
        )

    assert response.status_code == 200
    assert route.calls[0].request.headers["x-correlation-id"] == incoming
    assert response.headers["x-correlation-id"] == incoming
    assert tokens.correlation_ids == [incoming]


@respx.mock
@pytest.mark.asyncio
async def test_quote_without_the_header_generates_one_and_keeps_it():
    route = respx.post(f"{BASE_URL}/cotizaciones").mock(
        return_value=httpx.Response(200, json=QUOTE)
    )
    tokens = _Tokens()
    app = await _app_with_real_dispatch(tokens)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/envio/cotizar", json=PAYLOAD)

    assert response.status_code == 200
    forwarded = route.calls[0].request.headers["x-correlation-id"]
    assert str(uuid.UUID(forwarded)) == forwarded
    # The generated id is the one the middleware returns and the token request sees.
    assert response.headers["x-correlation-id"] == forwarded
    assert tokens.correlation_ids == [forwarded]
