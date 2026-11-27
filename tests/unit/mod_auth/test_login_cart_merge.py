"""Unit tests for login/MFA cart merge integration in mod-auth (SPEC-03 Req. 4, #184)."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.mod_auth.adapters.http.session_router import create_session_router
from src.modules.mod_auth.application.manage_session_use_case import ManageSessionUseCase
from src.shared.application.ports.cart_merger_port import CART_MERGED_MESSAGE, CartMergeResult
from src.shared.infrastructure.http.problem import register_problem_handlers

CUSTOMER_ID = uuid.uuid4()


def _build_client(cart_merger) -> tuple[TestClient, AsyncMock]:
    security = AsyncMock()
    security.login.return_value = {
        "accessToken": "tok-ok",
        "refreshToken": "rt-ok",
        "expiresIn": 900,
        "usuario": {"id": str(CUSTOMER_ID), "correo": "cliente@demo.pe"},
    }
    security.verify_mfa.return_value = {
        "accessToken": "tok-mfa",
        "refreshToken": "rt-mfa",
        "expiresIn": 900,
        "usuario": {"id": str(CUSTOMER_ID), "correo": "cliente@demo.pe"},
    }
    validator = AsyncMock()
    validator.validate_customer.return_value = {"sub": str(CUSTOMER_ID), "roles": ["CLIENTE"]}

    use_case = ManageSessionUseCase(
        security=security,
        validator=validator,
        cart_merger=cart_merger,
    )
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(
        create_session_router(lambda: use_case, cookie_secure=False),
        prefix="/api/v1",
    )
    return TestClient(app), cart_merger


def test_login_with_chat_sid_merges_anonymous_cart_and_returns_notice() -> None:
    merger = AsyncMock()
    merger.merge_anonymous_cart.return_value = CartMergeResult(
        merged=True,
        message=CART_MERGED_MESSAGE,
        adjustments=[
            {
                "sku": "SKU-A",
                "nombre": "Zapatilla A",
                "cantidadSolicitada": 5,
                "cantidadFinal": 3,
                "motivo": "STOCK_INSUFICIENTE",
            }
        ],
    )
    client, _ = _build_client(merger)
    client.cookies.set("chat_sid", "sid-anon-123")

    response = client.post(
        "/api/v1/sesion/login",
        json={"email": "cliente@demo.pe", "password": "secret"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["carritoFusionado"] is True
    assert body["avisoCarrito"] == CART_MERGED_MESSAGE
    assert len(body["ajustesCarrito"]) == 1
    merger.merge_anonymous_cart.assert_awaited_once_with(
        anonymous_sid="sid-anon-123",
        customer_id=CUSTOMER_ID,
    )


def test_login_without_chat_sid_does_not_call_merger() -> None:
    merger = AsyncMock()
    client, _ = _build_client(merger)

    response = client.post(
        "/api/v1/sesion/login",
        json={"email": "cliente@demo.pe", "password": "secret"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["carritoFusionado"] is False
    assert body["avisoCarrito"] is None
    assert body["ajustesCarrito"] == []
    merger.merge_anonymous_cart.assert_not_awaited()


def test_mfa_verify_with_chat_sid_merges_anonymous_cart() -> None:
    merger = AsyncMock()
    merger.merge_anonymous_cart.return_value = CartMergeResult(
        merged=True,
        message=CART_MERGED_MESSAGE,
    )
    client, _ = _build_client(merger)
    client.cookies.set("chat_sid", "sid-mfa-999")
    client.cookies.set("chat_chg", "challenge-tok", path="/api/v1/sesion")

    response = client.post(
        "/api/v1/sesion/mfa/verificar",
        json={"code": "123456"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["carritoFusionado"] is True
    assert body["avisoCarrito"] == CART_MERGED_MESSAGE
    merger.merge_anonymous_cart.assert_awaited_once_with(
        anonymous_sid="sid-mfa-999",
        customer_id=CUSTOMER_ID,
    )


def test_login_succeeds_even_if_cart_merger_raises() -> None:
    merger = AsyncMock()
    merger.merge_anonymous_cart.side_effect = RuntimeError("db glitch")
    client, _ = _build_client(merger)
    client.cookies.set("chat_sid", "sid-err")

    response = client.post(
        "/api/v1/sesion/login",
        json={"email": "cliente@demo.pe", "password": "secret"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["carritoFusionado"] is False
    assert body["avisoCarrito"] is None
