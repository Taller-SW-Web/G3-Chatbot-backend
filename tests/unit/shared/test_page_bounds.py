"""Page-number pagination has a hard ceiling and rejects out-of-range pages up front.

`page`/`pagina=1000000` used to reach the handler (and its DB/module calls).
The routers now validate `le=MAX_PAGE`, so FastAPI answers `400 VALIDACION`
before any use case, SQL query or G5/G6 call (#255).
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.mod_catalog.adapters.http.catalog_router import create_catalog_router
from src.modules.mod_chat.adapters.http.chatbot_router import create_chatbot_router
from src.modules.mod_orders.adapters.http.orders_router import create_orders_router
from src.modules.mod_postventa.adapters.http.claims_router import create_claims_router
from src.modules.mod_postventa.adapters.http.returns_router import create_returns_router
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.auth_guard import require_customer
from src.shared.infrastructure.http.dependencies.caller_identity import get_caller_identity
from src.shared.infrastructure.http.problem import register_problem_handlers

TOO_HIGH = 1_000_000
AUTH = {"Authorization": "Bearer unit-test-token"}


class NeverCalledService:
    """Records any call: a rejected page must never reach the use case."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def list_conversations(self, caller, pagina: int):
        self.calls.append(("list_conversations", pagina))
        return []

    async def search_conversations(self, caller, query: str, pagina: int):
        self.calls.append(("search_conversations", pagina))
        return []

    async def search(self, args, context):
        self.calls.append(("search", args.pagina))
        return {"texto": "", "total": 0, "bloques": []}

    async def list_orders(self, customer_id, *, holder_token: str, page: int = 1, **kwargs):
        self.calls.append(("list_orders", page))
        return {"items": [], "total": 0, "page": page, "pageSize": 10}

    async def list_claims(self, *, holder_token: str, page: int = 1, **kwargs):
        self.calls.append(("list_claims", page))
        return {"items": [], "total": 0, "page": page, "pageSize": 10}

    async def list_returns(self, *, holder_token: str, page: int = 1, **kwargs):
        self.calls.append(("list_returns", page))
        return {"items": [], "total": 0, "page": page, "pageSize": 10}


@pytest.fixture
def page_app():
    service = NeverCalledService()
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(create_chatbot_router(lambda: service, cookie_secure=True), prefix="/api/v1")
    app.include_router(create_catalog_router(lambda: service), prefix="/api/v1")
    app.include_router(create_orders_router(lambda: service), prefix="/api/v1")
    app.include_router(create_claims_router(lambda: service), prefix="/api/v1")
    app.include_router(create_returns_router(lambda: service), prefix="/api/v1")
    app.dependency_overrides[require_customer] = lambda: CallerIdentity(customer_id=uuid.uuid4())
    app.dependency_overrides[get_caller_identity] = lambda: None
    return app, service


@pytest.mark.parametrize(
    ("path", "param"),
    [
        ("/api/v1/chat/conversaciones", "pagina"),
        ("/api/v1/chat/conversaciones/buscar?q=zapatillas", "pagina"),
        ("/api/v1/catalogo/productos", "pagina"),
        ("/api/v1/pedidos", "page"),
        ("/api/v1/reclamos", "page"),
        ("/api/v1/devoluciones", "page"),
    ],
)
def test_page_over_the_ceiling_is_a_validation_error_without_calling_the_service(
    page_app, path: str, param: str
) -> None:
    app, service = page_app
    separator = "&" if "?" in path else "?"
    response = TestClient(app).get(f"{path}{separator}{param}={TOO_HIGH}", headers=AUTH)
    assert response.status_code == 400, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "VALIDACION"
    assert service.calls == []


def test_the_ceiling_itself_is_accepted(page_app) -> None:
    app, service = page_app
    response = TestClient(app).get(
        "/api/v1/catalogo/productos", params={"pagina": 1000}
    )
    assert response.status_code == 200, response.text
    assert service.calls == [("search", 1000)]
