"""Productos mock helpers (ADR-0017).

respx routes backed by seed fixtures in `tests/fixtures/products/`.
Switching mock<->real stays config-only: tests point the clients at the
mock base URL, production points them at the real module.
"""
import json
from functools import lru_cache
from pathlib import Path

import httpx
import respx

FIXTURES = (
    Path(__file__).resolve().parent.parent.parent
    / "src" / "adapters" / "outbound" / "fakes" / "seeds"
)


def load_fixture(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=1)
def load_catalog() -> list:
    return load_fixture("products_catalog.json")


def mock_product_search(base_url: str = "http://mock:4011"):
    """Paginated search over the 100-product catalog seed.

    Reads `pagina`/`tamanio` from the request query (filters are accepted
    but not applied: the mock paginates the full catalog).
    """

    def handler(request):
        params = request.url.params
        page = int(params.get("pagina", "1"))
        size = int(params.get("tamanio", "10"))
        catalog = load_catalog()
        start = (page - 1) * size
        return httpx.Response(
            200,
            json={
                "items": catalog[start : start + size],
                "pagina": page,
                "tamanio": size,
                "total": len(catalog),
            },
        )

    return respx.get(f"{base_url}/productos").mock(side_effect=handler)


def mock_product_detail(
    product_id: str = "prod-001", base_url: str = "http://mock:4011"
):
    return respx.get(f"{base_url}/productos/{product_id}").mock(
        return_value=httpx.Response(200, json=load_fixture("product_detail.json"))
    )


def mock_product_detail_not_found(
    product_id: str = "prod-missing", base_url: str = "http://mock:4011"
):
    return respx.get(f"{base_url}/productos/{product_id}").mock(
        return_value=httpx.Response(
            404, json={"code": "RECURSO_NO_ENCONTRADO", "title": "No encontrado"}
        )
    )


def mock_categories(base_url: str = "http://mock:4011"):
    return respx.get(f"{base_url}/categorias").mock(
        return_value=httpx.Response(200, json=load_fixture("categories.json"))
    )


def mock_brands(base_url: str = "http://mock:4011"):
    return respx.get(f"{base_url}/marcas").mock(
        return_value=httpx.Response(200, json=load_fixture("brands.json"))
    )


def mock_price(sku: str = "NIK-REV7-42-NEG", base_url: str = "http://mock:4011"):
    return respx.get(f"{base_url}/precios/skus/{sku}").mock(
        return_value=httpx.Response(200, json=load_fixture("price_sku.json"))
    )


def mock_prices_bulk(base_url: str = "http://mock:4011"):
    return respx.get(f"{base_url}/precios").mock(
        return_value=httpx.Response(200, json=load_fixture("prices_bulk.json"))
    )


def mock_availability(base_url: str = "http://mock:4011"):
    """Commercial projection (`status` per SKU, no quantities)."""
    return respx.get(f"{base_url}/inventario/disponibilidad/comercial").mock(
        return_value=httpx.Response(200, json=load_fixture("availability.json"))
    )


def mock_availability_verify(base_url: str = "http://mock:4011"):
    return respx.post(
        f"{base_url}/inventario/disponibilidad/comercial/verificar"
    ).mock(
        return_value=httpx.Response(200, json=load_fixture("availability_verify.json"))
    )


def mock_promotions_evaluate(base_url: str = "http://mock:4011"):
    return respx.post(f"{base_url}/promociones/evaluar").mock(
        return_value=httpx.Response(200, json=load_fixture("promotions_evaluate.json"))
    )


def mock_promotions_list(base_url: str = "http://mock:4011"):
    return respx.get(f"{base_url}/promociones").mock(
        return_value=httpx.Response(200, json=load_fixture("promotions.json"))
    )


def mock_module_down(
    method: str = "GET", path: str = "/productos", base_url: str = "http://mock:4011"
):
    route = respx.route(method=method, url=f"{base_url}{path}")
    route.mock(side_effect=httpx.ConnectError("mock is down"))
    return route
