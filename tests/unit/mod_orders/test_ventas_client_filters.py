"""`GET /pedidos/me`: estado, desde, hasta and pagination reach G5 (SPEC-17)."""
import anyio
import httpx
import respx

from src.shared.infrastructure.http.ventas_client import VentasClient

BASE_URL = "http://mock-ventas:4012"
HOLDER = "fake-access-customer-1"


@respx.mock
def test_list_customer_orders_sends_estado_desde_hasta_and_pagination() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"items": [], "total": 0, "page": 2, "pageSize": 5})

    respx.get(f"{BASE_URL}/api/v1/pedidos/me").mock(side_effect=handler)
    client = VentasClient(base_url=BASE_URL, timeout_seconds=5)

    anyio.run(
        lambda: client.list_customer_orders(
            HOLDER,
            estado="ENTREGADO",
            desde="2026-01-01",
            hasta="2026-12-31",
            page=2,
            page_size=5,
        )
    )

    assert seen == {
        "estado": "ENTREGADO",
        "desde": "2026-01-01",
        "hasta": "2026-12-31",
        "page": "2",
        "pageSize": "5",
    }
