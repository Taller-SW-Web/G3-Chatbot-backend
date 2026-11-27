"""G6 Productos contract: prices and promotions (OpenAPI 0.7.0-rc.3)."""
import json
import uuid
from datetime import datetime

import httpx
import pytest
import respx

from src.modules.mod_catalog.adapters.products_client import ProductsClient

BASE_URL = "http://mock-productos:4011/api/v1"


class StubServiceTokens:
    def __init__(self) -> None:
        self.requests: list[dict[str, str | None]] = []

    async def get_token(self, *, correlation_id=None, scope=None, audience=None) -> str:
        self.requests.append({"scope": scope, "audience": audience})
        return "svc-token"


def make_client(tokens=None) -> ProductsClient:
    return ProductsClient(
        base_url=BASE_URL,
        timeout_seconds=4.0,
        service_token_provider=tokens or StubServiceTokens(),
    )


@respx.mock
@pytest.mark.asyncio
async def test_bulk_prices_send_comma_separated_skus_and_unwrap_items(load_fixture):
    body = load_fixture("g6_precios.json")
    route = respx.get(f"{BASE_URL}/precios").mock(
        return_value=httpx.Response(200, json=body)
    )
    tokens = StubServiceTokens()

    result = await make_client(tokens).get_prices(["SKU-AUR-01", "SKU-RUN-42"])

    params = dict(route.calls[0].request.url.params)
    assert params == {"skus": "SKU-AUR-01,SKU-RUN-42", "canal": "CHATBOT"}
    assert result == body["items"]
    assert result[0]["precio_oferta"] == 109.9
    assert result[1]["channel_id"] is None
    assert tokens.requests == [{"scope": "precios:leer", "audience": "api-productos"}]


@respx.mock
@pytest.mark.asyncio
async def test_single_price_reads_the_documented_vigencia(load_fixture):
    body = load_fixture("g6_precio.json")
    route = respx.get(f"{BASE_URL}/precios/skus/SKU-AUR-01").mock(
        return_value=httpx.Response(200, json=body)
    )

    result = await make_client().get_price("SKU-AUR-01")

    assert dict(route.calls[0].request.url.params) == {"canal": "CHATBOT"}
    assert result == body
    assert result["vigencia_id"] == "VIG-2026-1101-AUR"
    assert result["origen"] == "SKU_OVERRIDE"


@respx.mock
@pytest.mark.asyncio
async def test_list_promotions_uses_vigentes_and_unwraps_items(load_fixture):
    body = load_fixture("g6_promociones.json")
    route = respx.get(f"{BASE_URL}/promociones").mock(
        return_value=httpx.Response(200, json=body)
    )

    result = await make_client().list_promotions()

    assert dict(route.calls[0].request.url.params) == {
        "canal": "CHATBOT",
        "vigentes": "true",
    }
    assert result == body["items"]
    assert result[0]["promotion_id"] == "PROMO-2026-11"


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_promotions_posts_the_documented_request(load_fixture):
    body = load_fixture("g6_promocion_evaluacion.json")
    route = respx.post(f"{BASE_URL}/promociones/evaluar").mock(
        return_value=httpx.Response(200, json=body)
    )
    tokens = StubServiceTokens()
    customer_ref = str(uuid.uuid4())
    lines = [{"sku": "SKU-AUR-01", "quantity": 1}]

    result = await make_client(tokens).evaluate_promotions(
        lines, coupon_code="CYBER10", customer_ref=customer_ref
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload["channel_id"] == "CHATBOT"
    assert payload["lines"] == lines
    assert payload["coupon_code"] == "CYBER10"
    assert payload["customer_ref"] == customer_ref
    assert datetime.fromisoformat(payload["at"])
    assert result == body
    assert result["result_amount"] == 109.9
    assert tokens.requests == [
        {"scope": "promociones:evaluar", "audience": "api-productos"}
    ]
