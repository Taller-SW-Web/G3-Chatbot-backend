"""G6 Inventario contract: commercial availability (OpenAPI 0.7.0-rc.3)."""
import json

import httpx
import pytest
import respx

from src.modules.mod_catalog.adapters.inventory_client import InventoryClient

BASE_URL = "http://mock-productos:4011/api/v1"


class StubServiceTokens:
    def __init__(self) -> None:
        self.requests: list[dict[str, str | None]] = []

    async def get_token(self, *, correlation_id=None, scope=None, audience=None) -> str:
        self.requests.append({"scope": scope, "audience": audience})
        return "svc-token"


def make_client(tokens=None) -> InventoryClient:
    return InventoryClient(
        base_url=BASE_URL,
        timeout_seconds=3.0,
        service_token_provider=tokens or StubServiceTokens(),
    )


@respx.mock
@pytest.mark.asyncio
async def test_availability_uses_the_documented_skus_and_canal(load_fixture):
    body = load_fixture("g6_disponibilidad.json")
    route = respx.get(f"{BASE_URL}/inventario/disponibilidad/comercial").mock(
        return_value=httpx.Response(200, json=body)
    )
    tokens = StubServiceTokens()

    result = await make_client(tokens).get_availability(["SKU-AUR-01", "SKU-RUN-42"])

    assert dict(route.calls[0].request.url.params) == {
        "skus": "SKU-AUR-01,SKU-RUN-42",
        "canal": "CHATBOT",
    }
    assert result == body["items"]
    assert [row["status"] for row in result] == ["DISPONIBLE", "STOCK_BAJO"]
    assert tokens.requests == [
        {"scope": "inventario:disponibilidad:leer", "audience": "api-productos"}
    ]


@respx.mock
@pytest.mark.asyncio
async def test_verify_quantities_posts_fulfillment_and_reads_items(load_fixture):
    body = load_fixture("g6_verificacion.json")
    route = respx.post(
        f"{BASE_URL}/inventario/disponibilidad/comercial/verificar"
    ).mock(return_value=httpx.Response(200, json=body))
    lines = [
        {"sku": "SKU-AUR-01", "quantity": 2},
        {"sku": "SKU-RUN-42", "quantity": 10},
    ]

    result = await make_client().verify_quantities(lines)

    assert json.loads(route.calls[0].request.content) == {
        "channel_id": "CHATBOT",
        "fulfillment": {"mode": "ENVIO_DOMICILIO"},
        "lines": lines,
    }
    assert result == body["items"]
    assert [row["result"] for row in result] == ["SUFICIENTE", "INSUFICIENTE"]
    assert result[1]["reason_code"] == "STOCK_BAJO"
