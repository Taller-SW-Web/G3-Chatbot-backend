"""Catalog and cart routers through TestClient (SPEC-06, SPEC-09, SPEC-10, SPEC-11).

Dependency overrides inject fake-backed use cases: no network, no database.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.application.use_cases.manage_cart_use_case import ManageCartUseCase
from src.application.use_cases.manage_catalog_use_case import ManageCatalogUseCase
from src.domain.value_objects.caller_identity import CallerIdentity
from src.infrastructure.config.container import (
    provide_cart_service,
    provide_catalog_service,
)
from src.infrastructure.main import create_app
from tests.fakes.fake_commerce import (
    FakeCarts,
    FakeCatalogPort,
    FakeConversations,
    FakeInventoryPort,
    FakeItems,
    FakeMessages,
    FakeUow,
)

CUSTOMER_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()
STATUSES = {"NIK-REV7-42-NEG": "DISPONIBLE"}


def make_app():
    conversations = FakeConversations()
    conversations.add_conversation(
        CONVERSATION_ID, customer_id=CUSTOMER_ID, context={})
    catalog_uc = ManageCatalogUseCase(
        FakeCatalogPort(), FakeInventoryPort(dict(STATUSES)), conversations,
        FakeUow(),
    )
    cart_uc = ManageCartUseCase(
        FakeCarts(), FakeItems(), FakeCatalogPort(),
        FakeInventoryPort(dict(STATUSES)), conversations, FakeMessages(),
        FakeUow(),
    )
    app = create_app()
    app.dependency_overrides[provide_catalog_service] = lambda: catalog_uc
    app.dependency_overrides[provide_cart_service] = lambda: cart_uc
    app.dependency_overrides[get_caller_identity] = (
        lambda: CallerIdentity(customer_id=CUSTOMER_ID)
    )
    return TestClient(app), cart_uc


def test_search_endpoint_returns_items_and_total():
    client, _ = make_app()
    response = client.get("/api/v1/catalogo/productos", params={"q": "Nike"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] > 0
    assert body["items"][0]["product_id"]


def test_detail_endpoint_missing_maps_to_409():
    client, _ = make_app()
    response = client.get("/api/v1/catalogo/productos/prod-missing")
    assert response.status_code == 409
    assert response.json()["code"] == "PRODUCTO_NO_DISPONIBLE"


def test_availability_endpoint_with_quantity_verdict():
    client, _ = make_app()
    response = client.get("/api/v1/catalogo/disponibilidad",
                          params={"sku": "NIK-REV7-42-NEG", "cantidad": 2})
    assert response.status_code == 200
    row = response.json()[0]
    assert row["status"] == "DISPONIBLE"
    assert row["veredicto"] == "SUFICIENTE"


def test_cart_flow_add_view_clear():
    client, _ = make_app()
    added = client.post("/api/v1/carrito/items",
                        json={"sku": "NIK-REV7-42-NEG", "cantidad": 1})
    assert added.status_code == 200
    assert "Agregué" in added.json()["mensaje"]
    view = client.get("/api/v1/carrito")
    assert view.status_code == 200
    assert view.json()["total"] > 0
    refused = client.request("DELETE", "/api/v1/carrito",
                             json={"confirmado": False})
    assert refused.status_code == 400
    cleared = client.request("DELETE", "/api/v1/carrito",
                             json={"confirmado": True})
    assert cleared.status_code == 200
    assert client.get("/api/v1/carrito").json()["lineas"] == []


def test_cart_requires_caller():
    client, cart_uc = make_app()
    client.app.dependency_overrides[get_caller_identity] = lambda: None
    response = client.get("/api/v1/carrito")
    assert response.status_code == 401
    assert response.json()["code"] == "REQUIERE_SESION"


def test_cart_patch_and_remove_item():
    client, _ = make_app()
    client.post("/api/v1/carrito/items",
                json={"sku": "NIK-REV7-42-NEG", "cantidad": 1})
    item_id = client.get("/api/v1/carrito").json()["lineas"][0]["item_id"]
    patched = client.patch(f"/api/v1/carrito/items/{item_id}",
                           json={"cantidad": 3})
    assert patched.status_code == 200
    assert patched.json()["carrito"]["lineas"][0]["cantidad"] == 3
    removed = client.delete(f"/api/v1/carrito/items/{item_id}")
    assert removed.status_code == 200
    assert client.get("/api/v1/carrito").json()["lineas"] == []


def test_cart_rejects_unknown_conversation():
    """An unowned/unknown conversation id is a 404, never a silent cart."""
    client, _ = make_app()
    response = client.get("/api/v1/carrito",
                          params={"conversacionId": str(uuid.uuid4())})
    assert response.status_code == 404
    assert response.json()["code"] == "RECURSO_NO_ENCONTRADO"


def test_add_without_variant_returns_409_problem():
    """POST needs talla/color: problem+json with `faltantes`, not a fake cart."""
    client, _ = make_app()
    response = client.post("/api/v1/carrito/items",
                           json={"product_id": "prod-001", "cantidad": 1})
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "VARIANTE_REQUERIDA"
    assert set(body["faltantes"]) == {"talla", "color"}
    assert body["product_id"] == "prod-001"
