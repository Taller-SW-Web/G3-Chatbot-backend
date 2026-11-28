"""The published OpenAPI describes the real runtime (backend#253).

Schema-level guarantees (security, Idempotency-Key, problem+json) plus a contract check:
real 400/401/409 responses validate against the published `Problem` schema.
`jsonschema` and `openapi-spec-validator` are optional test tools: their tests skip when absent.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from src.infrastructure.main import create_app
from src.modules.mod_cart.application.manage_cart_use_case import ManageCartUseCase
from src.modules.mod_cart.module import provide_cart_service
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.caller_identity import get_caller_identity
from src.shared.infrastructure.http.openapi import BEARER_SCHEME, PROBLEM_MEDIA_TYPE
from tests.fakes.fake_commerce import (
    FakeCarts,
    FakeCatalogPort,
    FakeConversationContext,
    FakeInventoryPort,
    FakeItems,
    FakeUow,
)

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
IDEMPOTENT_OPERATIONS = {
    ("post", "/api/v1/checkout"),
    ("post", "/api/v1/checkout/{checkout_id}/pago"),
    ("post", "/api/v1/devoluciones"),
    ("post", "/api/v1/reclamos"),
    ("post", "/api/v1/pedidos/{pedido_id}/reenviar-confirmacion"),
    ("post", "/api/v1/sugerencias"),
}


@pytest.fixture(scope="module")
def schema() -> dict:
    return TestClient(create_app()).get("/openapi.json").json()


def _operations(schema: dict):
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method in HTTP_METHODS:
                yield method, path, operation


def test_bearer_scheme_is_declared_and_never_global(schema: dict) -> None:
    assert schema["components"]["securitySchemes"][BEARER_SCHEME] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Customer access token: `Authorization: Bearer <JWT>`.",
    }
    assert "security" not in schema


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("post", "/api/v1/checkout", [{BEARER_SCHEME: []}]),
        ("get", "/api/v1/pedidos", [{BEARER_SCHEME: []}]),
        ("get", "/api/v1/sesion/perfil", [{BEARER_SCHEME: []}]),
        ("delete", "/api/v1/carrito/cupon", [{BEARER_SCHEME: []}]),
        # Optional auth: anonymous visitors (chat_sid cookie) are accepted too.
        ("get", "/api/v1/carrito", [{}, {BEARER_SCHEME: []}]),
        ("post", "/api/v1/chat/conversaciones", [{}, {BEARER_SCHEME: []}]),
        ("post", "/api/v1/chat/conversaciones/{conversation_id}/mensajes", [{}, {BEARER_SCHEME: []}]),
        # Public routes carry no security requirement.
        ("get", "/api/v1/catalogo/productos", None),
        ("post", "/api/v1/sesion/login", None),
        ("get", "/health", None),
    ],
)
def test_security_follows_the_real_auth_dependency(
    schema: dict, method: str, path: str, expected: list | None
) -> None:
    assert schema["paths"][path][method].get("security") == expected


def test_secured_operations_document_401_and_403(schema: dict) -> None:
    for _, path, operation in _operations(schema):
        if "security" in operation:
            assert {"401", "403"} <= set(operation["responses"]), path


def test_idempotency_key_is_required_where_the_runtime_requires_it(schema: dict) -> None:
    declared = set()
    for method, path, operation in _operations(schema):
        for parameter in operation.get("parameters", []):
            if parameter["name"] == "Idempotency-Key":
                declared.add((method, path))
                assert parameter["in"] == "header"
                assert parameter["required"] is True
                assert parameter["description"]
    assert declared == IDEMPOTENT_OPERATIONS


def test_no_default_422_validation_error_is_published(schema: dict) -> None:
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert "ValidationError" not in schema["components"]["schemas"]
    for _, path, operation in _operations(schema):
        if "422" in operation["responses"]:
            content = operation["responses"]["422"]["content"]
            assert set(content) == {PROBLEM_MEDIA_TYPE}, path
        if "parameters" in operation or "requestBody" in operation:
            assert "400" in operation["responses"], path


def test_every_error_response_is_a_problem(schema: dict) -> None:
    for _, path, operation in _operations(schema):
        for status, response in operation["responses"].items():
            if status.startswith(("4", "5")):
                assert response["content"] == {
                    PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}
                }, (path, status)


def test_published_document_is_a_valid_openapi_spec(schema: dict) -> None:
    import openapi_spec_validator as validator
    validator.validate(schema)


# --- Runtime responses validate against the published Problem schema -------------------------

CUSTOMER_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()


def _client() -> TestClient:
    context = FakeConversationContext()
    context.add_conversation(CONVERSATION_ID, customer_id=CUSTOMER_ID, context={})
    statuses = {"NIK-REV7-42-NEG": "DISPONIBLE"}
    cart = ManageCartUseCase(
        FakeCarts(), FakeItems(), FakeCatalogPort(), FakeInventoryPort(dict(statuses)),
        context, FakeUow(),
    )
    app = create_app()
    app.dependency_overrides[provide_cart_service] = lambda: cart
    return TestClient(app)


def _assert_problem(response, status: int, schema: dict) -> None:
    import jsonschema
    assert response.status_code == status
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    problem_schema = schema["components"]["schemas"]["Problem"]
    jsonschema.validate(response.json(), problem_schema)
    assert str(status) in _responses_for_known_paths(schema)


def _responses_for_known_paths(schema: dict) -> set[str]:
    return {
        status for _, _, operation in _operations(schema) for status in operation["responses"]
    }


def test_validation_error_is_a_published_400_problem(schema: dict) -> None:
    response = _client().post("/api/v1/sesion/login", json={})

    _assert_problem(response, 400, schema)
    assert response.json()["code"] == "VALIDACION"
    assert "400" in schema["paths"]["/api/v1/sesion/login"]["post"]["responses"]


def test_missing_session_is_a_published_401_problem(schema: dict) -> None:
    response = _client().delete("/api/v1/carrito/cupon")

    _assert_problem(response, 401, schema)
    assert response.json()["code"] == "REQUIERE_SESION"
    assert "401" in schema["paths"]["/api/v1/carrito/cupon"]["delete"]["responses"]


def test_conflict_is_a_published_409_problem(schema: dict) -> None:
    client = _client()
    client.app.dependency_overrides[get_caller_identity] = lambda: CallerIdentity(
        customer_id=CUSTOMER_ID
    )
    response = client.post("/api/v1/carrito/items", json={"product_id": "prod-001", "cantidad": 1})

    _assert_problem(response, 409, schema)
    assert response.json()["code"] == "VARIANTE_REQUERIDA"
    assert "409" in schema["paths"]["/api/v1/carrito/items"]["post"]["responses"]
