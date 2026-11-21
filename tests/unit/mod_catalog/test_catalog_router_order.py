"""GET /catalogo/productos carries `orden` into the search use case."""
import httpx
import pytest
from fastapi import FastAPI

from src.modules.mod_catalog.adapters.http.catalog_router import create_catalog_router
from src.shared.infrastructure.http.problem import register_problem_handlers


class StubCatalog:
    def __init__(self):
        self.arguments = None

    async def search(self, args, context):
        self.arguments = args
        return {
            "texto": "ok",
            "total": 2,
            "bloques": [
                {
                    "tipo": "CARRUSEL_PRODUCTOS",
                    "datos": {
                        "productos": [
                            {"product_id": "p1", "name": "Polos", "price": 10.0},
                            {"product_id": "p2", "name": "Zapatillas", "price": 20.0},
                        ]
                    },
                }
            ],
        }


def app_with(service):
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(create_catalog_router(lambda: service), prefix="/api/v1")
    return app


@pytest.mark.asyncio
async def test_lowercase_order_is_forwarded_to_the_search():
    service = StubCatalog()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service)), base_url="http://test"
    ) as client:
        res = await client.get("/api/v1/catalogo/productos", params={"orden": "precio_asc"})
    assert res.status_code == 200
    assert service.arguments.orden == "precio_asc"


@pytest.mark.asyncio
async def test_unknown_order_is_rejected_as_validation():
    service = StubCatalog()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service)), base_url="http://test"
    ) as client:
        res = await client.get("/api/v1/catalogo/productos", params={"orden": "caro_primero"})
    assert res.status_code == 400
    assert res.json()["code"] == "VALIDACION"
