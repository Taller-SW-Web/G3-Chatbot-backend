"""Unit tests for H4-S6: canonical `require_customer` guard and login/chat rate limits."""
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.inbound.http.cart_router import create_cart_router
from src.adapters.inbound.http.chatbot_router import create_chatbot_router
from src.adapters.inbound.http.checkout_router import create_checkout_router
from src.adapters.inbound.http.dependencies.auth_guard import require_customer
from src.adapters.inbound.http.dependencies.caller_identity import hash_chat_sid
from src.adapters.inbound.http.dependencies.checkout_guard import (
    require_customer as legacy_require_customer,
)
from src.adapters.inbound.http.dependencies.jwt_validator import get_access_token_validator
from src.adapters.inbound.http.dependencies.rate_limiter import (
    CHAT_MESSAGES_PER_MINUTE,
    LOGIN_PER_IP,
    InMemoryRateLimiter,
)
from src.adapters.inbound.http.orders_router import create_orders_router
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.inbound.http.session_router import create_session_router
from src.adapters.inbound.http.shipping_router import create_shipping_router
from src.application.dto.conversation_events import FinalEvent, MessageAcceptedEvent
from src.application.use_cases.manage_session_use_case import InvalidCredentialsError
from src.domain.entities.message import Message, MessageRole
from src.ports.outbound.token_validator_port import InvalidTokenError

CUSTOMER_ID = uuid.UUID("00000000-0000-0000-0000-000000000077")
CHECKOUT_ID = uuid.UUID("00000000-0000-0000-0000-000000000088")
CONVERSATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")
CSRF = {"X-Requested-With": "XMLHttpRequest"}


class _FakeValidator:
    async def validate_customer(self, token: str) -> dict:
        if token == "valid-customer-token":
            return {"sub": str(CUSTOMER_ID), "roles": ["CLIENTE"]}
        raise InvalidTokenError("Token expired or invalid signature")


class _StubService:
    async def list_orders(self, *args, **kwargs):
        return {"items": [], "page": 1, "pageSize": 10, "total": 0}

    async def get_order_detail(self, *args, **kwargs):
        return {}

    async def get_order_tracking(self, *args, **kwargs):
        return {}

    async def resend_confirmation(self, *args, **kwargs):
        return {"pedidoId": "PED-1", "estado": "ENVIADO", "reenviosRestantes": 1}

    async def list_addresses(self, *args, **kwargs):
        return {"direcciones": []}

    async def quote_shipping(self, *args, **kwargs):
        return {
            "coberturaDisponible": True,
            "costoEnvio": 12.5,
            "moneda": "PEN",
            "plazoEstimadoDias": 2,
        }

    async def profile(self, holder_token: str, customer_id: str):
        return {"id": customer_id, "celularVerificado": True, "ultimoDocumento": None}

    async def login(self, email: str, password: str, *, canal: str | None = None):
        if password == "wrong":
            raise InvalidCredentialsError()
        return {"accessToken": "tok", "expiresIn": 900, "usuario": {"id": str(CUSTOMER_ID)}}

    async def apply_coupon(self, *args, **kwargs):
        return {
            "texto": "Aplicado",
            "cupon": {"codigo": "RUN10"},
            "carrito": {"lineas": [], "subtotal": 100.0, "descuentos": [], "total": 90.0},
        }

    async def remove_coupon(self, *args, **kwargs):
        return {
            "texto": "Eliminado",
            "carrito": {"lineas": [], "subtotal": 100.0, "descuentos": [], "total": 100.0},
        }


def _build_protected_app(limiter: InMemoryRateLimiter | None = None) -> TestClient:
    service = _StubService()
    app = FastAPI()
    register_problem_handlers(app)
    app.dependency_overrides[get_access_token_validator] = _FakeValidator
    app.include_router(
        create_session_router(
            lambda: service,
            cookie_secure=False,
            rate_limiter=limiter,
        ),
        prefix="/api/v1",
    )
    app.include_router(create_checkout_router(lambda: service), prefix="/api/v1")
    app.include_router(create_orders_router(lambda: service), prefix="/api/v1")
    app.include_router(create_shipping_router(lambda: service), prefix="/api/v1")
    app.include_router(create_cart_router(lambda: service), prefix="/api/v1")
    return TestClient(app)


def test_checkout_guard_reexports_canonical_require_customer() -> None:
    assert legacy_require_customer is require_customer


@pytest.mark.parametrize(
    ("method", "path", "json_body", "extra_headers"),
    [
        (
            "POST",
            "/api/v1/checkout",
            {
                "cartId": str(uuid.uuid4()),
                "direccion": {
                    "departamento": "Lima",
                    "provincia": "Lima",
                    "distrito": "Miraflores",
                    "direccionLinea1": "Av. Larco 123",
                },
                "contacto": {
                    "nombres": "Ana",
                    "apellidos": "Perez",
                    "celular": "+51987654321",
                    "tipoDocumento": "DNI",
                    "numeroDocumento": "12345678",
                },
            },
            {"Idempotency-Key": "idem-1"},
        ),
        ("GET", f"/api/v1/checkout/{CHECKOUT_ID}", None, {}),
        (
            "POST",
            f"/api/v1/checkout/{CHECKOUT_ID}/pago",
            {
                "numeroTarjeta": "4557880000001111",
                "vencimiento": "12/29",
                "cvv": "123",
                "titular": "ANA PEREZ",
            },
            {"Idempotency-Key": "idem-pay-1"},
        ),
        ("GET", "/api/v1/pedidos", None, {}),
        ("GET", "/api/v1/pedidos/PED-001", None, {}),
        ("GET", "/api/v1/pedidos/PED-001/seguimiento", None, {}),
        ("POST", "/api/v1/pedidos/PED-001/reenviar-confirmacion", None, {"Idempotency-Key": "idem-2"}),
        ("GET", "/api/v1/direcciones", None, {}),
        (
            "POST",
            "/api/v1/envio/cotizar",
            {
                "destinatario": "Ana Perez",
                "direccion": "Av. Larco 123",
                "distrito": "Miraflores",
            },
            {},
        ),
        ("GET", "/api/v1/sesion/perfil", None, {}),
        ("POST", "/api/v1/carrito/cupon", {"codigo": "RUN10"}, {}),
        ("DELETE", "/api/v1/carrito/cupon", None, {}),
    ],
)
def test_protected_endpoints_reject_unauthenticated_and_anonymous_visitors_with_requiere_sesion(
    method: str,
    path: str,
    json_body: dict | None,
    extra_headers: dict[str, str],
) -> None:
    client = _build_protected_app()

    # 1. Completely unauthenticated (no cookie, no Authorization header)
    res_no_auth = client.request(method, path, json=json_body, headers=extra_headers)
    assert res_no_auth.status_code == 401
    assert res_no_auth.json()["code"] == "REQUIERE_SESION"

    # 2. Anonymous visitor with chat_sid cookie
    client.cookies.set("chat_sid", "anonymous-visitor-cookie")
    res_anon = client.request(method, path, json=json_body, headers=extra_headers)
    assert res_anon.status_code == 401
    assert res_anon.json()["code"] == "REQUIERE_SESION"

    # 3. Invalid or expired Bearer token -> 401 TOKEN_INVALIDO
    res_bad_token = client.request(
        method,
        path,
        json=json_body,
        headers={**extra_headers, "Authorization": "Bearer expired-or-tampered"},
    )
    assert res_bad_token.status_code == 401
    assert res_bad_token.json()["code"] == "TOKEN_INVALIDO"


def test_protected_endpoints_allow_valid_customer_token() -> None:
    client = _build_protected_app()
    auth = {"Authorization": "Bearer valid-customer-token"}

    assert client.get("/api/v1/sesion/perfil", headers=auth).status_code == 200
    assert client.get("/api/v1/pedidos", headers=auth).status_code == 200
    assert client.get("/api/v1/direcciones", headers=auth).status_code == 200
    assert (
        client.post(
            "/api/v1/envio/cotizar",
            json={
                "destinatario": "Ana Perez",
                "direccion": "Av. Larco 123",
                "distrito": "Miraflores",
            },
            headers=auth,
        ).status_code
        == 200
    )
    assert client.post("/api/v1/carrito/cupon", json={"codigo": "RUN10"}, headers=auth).status_code == 200
    assert client.delete("/api/v1/carrito/cupon", headers=auth).status_code == 200


def test_login_rate_limits_after_ten_attempts_per_ip() -> None:
    limiter = InMemoryRateLimiter()
    client = _build_protected_app(limiter=limiter)
    headers = {**CSRF, "X-Forwarded-For": "198.51.100.42"}
    payload = {"email": "cliente@correo.pe", "password": "wrong"}

    for _ in range(LOGIN_PER_IP.limit):
        res = client.post("/api/v1/sesion/login", json=payload, headers=headers)
        assert res.status_code == 401
        assert res.json()["code"] == "CREDENCIALES_INVALIDAS"

    blocked = client.post("/api/v1/sesion/login", json=payload, headers=headers)
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "DEMASIADAS_SOLICITUDES"
    assert blocked.json()["retryAfter"] >= 1
    assert blocked.headers["retry-after"] == str(blocked.json()["retryAfter"])

    # Another IP is not blocked
    other_ip = {**CSRF, "X-Forwarded-For": "203.0.113.99"}
    allowed = client.post("/api/v1/sesion/login", json=payload, headers=other_ip)
    assert allowed.status_code == 401


class _FakeChatTurnUseCase:
    def __init__(self) -> None:
        self.count = 0

    @asynccontextmanager
    async def scope(self):
        yield self

    async def send_message(self, caller, conversation_id, text: str):
        self.count += 1
        now = datetime(2026, 11, 6, tzinfo=timezone.utc)
        user_msg = Message(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            role=MessageRole.CUSTOMER,
            text=text,
            blocks=None,
            created_at=now,
        )
        bot_msg = Message(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            text="ok",
            blocks=None,
            created_at=now,
        )

        async def _stream():
            yield MessageAcceptedEvent(user_msg)
            yield FinalEvent(bot_msg)

        return _stream()


def test_chat_messages_rate_limited_at_twenty_per_minute_rn_cnv_17() -> None:
    limiter = InMemoryRateLimiter()
    turn_uc = _FakeChatTurnUseCase()
    app = FastAPI()
    register_problem_handlers(app)
    app.dependency_overrides[get_access_token_validator] = _FakeValidator
    app.include_router(
        create_chatbot_router(
            lambda: _StubService(),
            cookie_secure=False,
            turn_scope=turn_uc.scope,
            rate_limiter=limiter,
        ),
        prefix="/api/v1",
    )
    client = TestClient(app)
    client.cookies.set("chat_sid", f"sid-{hash_chat_sid('visitor-1')}")
    url = f"/api/v1/chat/conversaciones/{CONVERSATION_ID}/mensajes"

    for _ in range(CHAT_MESSAGES_PER_MINUTE.limit):
        res = client.post(url, json={"texto": "hola"})
        assert res.status_code == 200

    blocked = client.post(url, json={"texto": "mensaje 21"})
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "DEMASIADAS_SOLICITUDES"
    assert blocked.json()["retryAfter"] >= 1
    assert blocked.headers["retry-after"] == str(blocked.json()["retryAfter"])
    assert turn_uc.count == CHAT_MESSAGES_PER_MINUTE.limit

    # Authenticated customer has their own independent quota
    res_authed = client.post(
        url,
        json={"texto": "hola con sesion"},
        headers={"Authorization": "Bearer valid-customer-token"},
    )
    assert res_authed.status_code == 200
