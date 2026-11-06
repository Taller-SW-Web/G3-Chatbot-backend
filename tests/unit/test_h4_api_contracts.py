"""Unit tests for Hito 4 API contracts (H4-A1: coupons, shipping, checkout, orders)."""
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.adapters.inbound.http.cart_router import create_cart_router
from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.adapters.inbound.http.orders_router import OrderError, create_orders_router
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.inbound.http.shipping_router import ShippingError, create_shipping_router
from src.application.dto.commerce_contracts_dto import (
    CHECKOUT_ERROR_STATUS,
    COUPON_REJECTION_MESSAGES,
    ORDERS_ERROR_STATUS,
    PRODUCTS_TO_COUPON_REASON,
    SHIPPING_ERROR_STATUS,
    ApplyCouponRequest,
    BuyerContactDTO,
    CheckoutSessionResponse,
    CouponRejectionReason,
    CreateCheckoutRequest,
    PaymentResultResponse,
    ProcessPaymentRequest,
    ShippingQuoteRequest,
    validate_buyer_document,
)
from src.application.use_cases.manage_cart_use_case import CartError
from src.domain.services.status_mapper import map_order_status_label
from src.domain.services.tracking_mapper import sanitize_tracking_payload
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError

CUSTOMER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
AUTH_HEADER = {"Authorization": "Bearer valid-customer-jwt"}


# ---------------------------------------------------------------------------
# 1. Buyer Document Validation (`SPEC-12` Req. 1, agreement A14)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("doc_type", "doc_num"),
    [
        ("DNI", "72458912"),
        ("dni", " 01234567 "),
        ("RUC", "10724589123"),
        ("RUC", "15724589123"),
        ("RUC", "17724589123"),
        ("RUC", "20724589123"),
        ("CE", "A1234567"),
        ("CE", "AB1234567890"),
        ("PASAPORTE", "A12345"),
        ("PASAPORTE", "PE1234567890"),
    ],
)
def test_validate_buyer_document_accepts_valid_rules(doc_type: str, doc_num: str) -> None:
    normalized_type, normalized_num = validate_buyer_document(doc_type, doc_num)
    assert normalized_type == doc_type.strip().upper()
    assert normalized_num == doc_num.strip()


@pytest.mark.parametrize(
    ("doc_type", "doc_num"),
    [
        ("DNI", "1234567"),  # 7 digits
        ("DNI", "123456789"),  # 9 digits
        ("DNI", "1234567A"),  # non-numeric
        ("RUC", "30458912345"),  # invalid prefix 30 (SPEC-12 scenario)
        ("RUC", "2045891234"),  # 10 digits
        ("CE", "1234567"),  # 7 chars (SPEC-12 scenario: CE requires 8..12)
        ("CE", "1234567890123"),  # 13 chars
        ("PASAPORTE", "A1234"),  # 5 chars
        ("BREVETE", "12345678"),  # unsupported document type
        ("DNI", ""),  # empty
    ],
)
def test_validate_buyer_document_rejects_invalid_rules(doc_type: str, doc_num: str) -> None:
    with pytest.raises(ValueError, match="DATO_INVALIDO"):
        validate_buyer_document(doc_type, doc_num)


def test_buyer_contact_dto_normalizes_and_validates() -> None:
    contact = BuyerContactDTO(
        nombreCompleto="  Juan Pérez  ",
        tipoDocumento="ruc",
        numeroDocumento=" 20123456789 ",
    )
    assert contact.nombreCompleto == "Juan Pérez"
    assert contact.tipoDocumento == "RUC"
    assert contact.numeroDocumento == "20123456789"


# ---------------------------------------------------------------------------
# 2. Coupons Contract (`SPEC-13`, `POST/DELETE /api/v1/carrito/cupon`)
# ---------------------------------------------------------------------------


def test_apply_coupon_request_normalizes_code() -> None:
    req = ApplyCouponRequest(codigo="  run10 ")
    assert req.codigo == "RUN10"
    assert req.confirmarReemplazo is False

    with pytest.raises(ValidationError):
        ApplyCouponRequest(codigo="   ")


def test_coupon_rejection_reasons_and_messages_complete() -> None:
    assert PRODUCTS_TO_COUPON_REASON["CUPON_NO_ENCONTRADO"] is CouponRejectionReason.INEXISTENTE
    assert PRODUCTS_TO_COUPON_REASON["MONTO_MINIMO_NO_ALCANZADO"] is CouponRejectionReason.MONTO_MINIMO
    for reason in CouponRejectionReason:
        assert reason in COUPON_REJECTION_MESSAGES


class _FakeCouponCartService:
    def __init__(self) -> None:
        self.error: CartError | None = None

    async def apply_coupon(
        self,
        caller: CallerIdentity,
        conversacion_id: uuid.UUID | None,
        *,
        codigo: str,
        confirmar_reemplazo: bool = False,
    ) -> dict[str, Any]:
        if self.error:
            raise self.error
        return {
            "texto": f"Cupón {codigo} aplicado.",
            "cupon": {"codigo": codigo, "descuento": 30.0, "moneda": "PEN", "promocionId": "P-1"},
            "bloques": [
                {
                    "datos": {
                        "lineas": [],
                        "subtotal": 300.0,
                        "descuentos": [{"nombre": f"Cupón {codigo}", "monto": 30.0}],
                        "costoEnvio": None,
                        "total": 270.0,
                        "avisos": [],
                    }
                }
            ],
        }

    async def remove_coupon(
        self,
        caller: CallerIdentity,
        conversacion_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        if self.error:
            raise self.error
        return {
            "texto": "Cupón retirado.",
            "bloques": [
                {
                    "datos": {
                        "lineas": [],
                        "subtotal": 300.0,
                        "descuentos": [],
                        "costoEnvio": None,
                        "total": 300.0,
                        "avisos": [],
                    }
                }
            ],
        }


def _build_cart_app(service: _FakeCouponCartService, authenticated: bool = True) -> TestClient:
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(create_cart_router(lambda: service), prefix="/api/v1")
    app.dependency_overrides[get_caller_identity] = lambda: (
        CallerIdentity(customer_id=CUSTOMER_ID)
        if authenticated
        else CallerIdentity(anonymous_sid="anon-hash")
    )
    return TestClient(app)


def test_coupon_endpoints_require_authenticated_customer() -> None:
    client = _build_cart_app(_FakeCouponCartService(), authenticated=False)
    r_post = client.post("/api/v1/carrito/cupon", json={"codigo": "RUN10"})
    assert r_post.status_code == 401
    assert r_post.json()["code"] == "REQUIERE_SESION"

    r_del = client.delete("/api/v1/carrito/cupon")
    assert r_del.status_code == 401
    assert r_del.json()["code"] == "REQUIERE_SESION"


def test_coupon_endpoints_apply_and_remove_and_error_shapes() -> None:
    service = _FakeCouponCartService()
    client = _build_cart_app(service, authenticated=True)

    r_ok = client.post("/api/v1/carrito/cupon", json={"codigo": " run10 "})
    assert r_ok.status_code == 200
    body = r_ok.json()
    assert body["cupon"]["codigo"] == "RUN10"
    assert body["carrito"]["total"] == 270.0

    # Rejected coupon with reason and missing amount (SPEC-13 Req. 2)
    service.error = CartError(
        "CUPON_INVALIDO",
        "Este cupón aplica para compras desde S/ 200.00. Te faltan S/ 50.00.",
        data={"motivo": "MONTO_MINIMO", "montoMinimo": 200.0, "montoFaltante": 50.0},
    )
    r_invalid = client.post("/api/v1/carrito/cupon", json={"codigo": "RUN10"})
    assert r_invalid.status_code == 422
    problem = r_invalid.json()
    assert problem["code"] == "CUPON_INVALIDO"
    assert problem["motivo"] == "MONTO_MINIMO"
    assert problem["montoFaltante"] == 50.0

    # Rate limit exceeded (SPEC-13 Req. 5)
    service.error = CartError(
        "DEMASIADAS_SOLICITUDES", "Demasiados intentos, espera unos minutos."
    )
    r_rate = client.post("/api/v1/carrito/cupon", json={"codigo": "RUN10"})
    assert r_rate.status_code == 429
    assert r_rate.json()["code"] == "DEMASIADAS_SOLICITUDES"

    # Remove coupon
    service.error = None
    r_del = client.delete("/api/v1/carrito/cupon")
    assert r_del.status_code == 200
    assert r_del.json()["carrito"]["total"] == 300.0


# ---------------------------------------------------------------------------
# 3. Shipping & Saved Addresses Contract (`SPEC-12`)
# ---------------------------------------------------------------------------


def test_shipping_quote_request_accepts_ventas_and_ui_aliases() -> None:
    from_ui = ShippingQuoteRequest(
        nombreCompleto="Juan Pérez",
        direccionExacta="Av. Larco 1234, dpto. 502",
        distrito="Miraflores",
        referencia="Frente al parque",
    )
    assert from_ui.destinatario == "Juan Pérez"
    assert from_ui.direccion == "Av. Larco 1234, dpto. 502"

    from_ventas = ShippingQuoteRequest(
        destinatario="María Gómez",
        direccion="Jr. de la Unión 500",
        distrito="Cercado de Lima",
    )
    assert from_ventas.nombreCompleto == "María Gómez"
    assert from_ventas.direccionExacta == "Jr. de la Unión 500"

    with pytest.raises(ValidationError):
        ShippingQuoteRequest(
            nombreCompleto="Juan",
            direccionExacta="123",  # < 5 chars (SPEC-12 Req. 2)
            distrito="Miraflores",
        )


class _FakeShippingService:
    def __init__(self) -> None:
        self.quote_result: dict[str, Any] = {
            "coberturaDisponible": True,
            "costoEnvio": 12.50,
            "moneda": "PEN",
            "plazoEstimadoDiasHabiles": 1,
            "fechaEstimadaEntrega": "2026-11-06",
            "nombreZona": "Lima Metropolitana",
        }
        self.error: Exception | None = None

    async def list_addresses(
        self, customer_id: Any, *, holder_token: str
    ) -> dict[str, Any]:
        if self.error:
            raise self.error
        return {
            "direcciones": [
                {
                    "id": "dir-1",
                    "etiqueta": "Casa",
                    "destinatario": "Juan Pérez",
                    "distrito": "Miraflores",
                    "direccion": "Av. Larco 1234",
                    "predeterminada": True,
                }
            ]
        }

    async def quote_shipping(
        self, customer_id: Any, payload: ShippingQuoteRequest
    ) -> dict[str, Any]:
        if self.error:
            raise self.error
        return self.quote_result


def _build_shipping_app(service: _FakeShippingService, authenticated: bool = True) -> TestClient:
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(create_shipping_router(lambda: service), prefix="/api/v1")
    app.dependency_overrides[get_caller_identity] = lambda: (
        CallerIdentity(customer_id=CUSTOMER_ID) if authenticated else None
    )
    return TestClient(app)


def test_shipping_endpoints_contract_and_error_translation() -> None:
    service = _FakeShippingService()
    client = _build_shipping_app(service, authenticated=True)

    r_dirs = client.get("/api/v1/direcciones", headers=AUTH_HEADER)
    assert r_dirs.status_code == 200
    assert r_dirs.json()["direcciones"][0]["distrito"] == "Miraflores"

    r_quote = client.post(
        "/api/v1/envio/cotizar",
        headers=AUTH_HEADER,
        json={
            "nombreCompleto": "Juan Pérez",
            "direccionExacta": "Av. Larco 1234",
            "distrito": "Miraflores",
        },
    )
    assert r_quote.status_code == 200
    assert r_quote.json()["costoEnvio"] == 12.50
    assert r_quote.json()["plazoEstimadoDias"] == 1

    for missing_field in ("costoEnvio", "plazoEstimadoDiasHabiles"):
        service.quote_result = {
            "coberturaDisponible": True,
            "costoEnvio": 12.50,
            "plazoEstimadoDiasHabiles": 1,
        }
        del service.quote_result[missing_field]
        response = client.post(
            "/api/v1/envio/cotizar",
            headers=AUTH_HEADER,
            json={
                "destinatario": "Juan Pérez",
                "direccion": "Av. Larco 1234",
                "distrito": "Miraflores",
            },
        )
        assert response.status_code == 503
        assert response.json()["code"] == "MODULO_NO_DISPONIBLE"
        assert response.json()["modulo"] == "dispatch"

    # No coverage -> 422 SIN_COBERTURA
    service.quote_result = {"coberturaDisponible": False}
    r_no_cov = client.post(
        "/api/v1/envio/cotizar",
        headers=AUTH_HEADER,
        json={
            "destinatario": "Juan Pérez",
            "direccion": "Calle Real 100",
            "distrito": "Chosica",
        },
    )
    assert r_no_cov.status_code == 422
    assert r_no_cov.json()["code"] == "SIN_COBERTURA"
    assert r_no_cov.json()["distrito"] == "Chosica"

    # Despacho error translation (DESP_ERROR_LIMITE_COTIZACION -> 429 DEMASIADAS_SOLICITUDES)
    service.error = ShippingError("DESP_ERROR_LIMITE_COTIZACION", "Límite excedido")
    r_limit = client.post(
        "/api/v1/envio/cotizar",
        headers=AUTH_HEADER,
        json={
            "destinatario": "Juan Pérez",
            "direccion": "Av. Larco 1234",
            "distrito": "Miraflores",
        },
    )
    assert r_limit.status_code == 429
    assert r_limit.json()["code"] == "DEMASIADAS_SOLICITUDES"

    # Despacho timeout -> 503 MODULO_NO_DISPONIBLE
    service.error = ModuleUnavailableError("dispatch", "timeout")
    r_down = client.post(
        "/api/v1/envio/cotizar",
        headers=AUTH_HEADER,
        json={
            "destinatario": "Juan Pérez",
            "direccion": "Av. Larco 1234",
            "distrito": "Miraflores",
        },
    )
    assert r_down.status_code == 503
    assert r_down.json()["code"] == "MODULO_NO_DISPONIBLE"
    assert r_down.json()["modulo"] == "dispatch"


# ---------------------------------------------------------------------------
# 4. Checkout & Payment Simulation DTO Contracts (`SPEC-14`, `SPEC-15`)
# ---------------------------------------------------------------------------


def test_checkout_and_payment_contract_dtos() -> None:
    req = CreateCheckoutRequest(
        guardarDireccion=True,
        direccion={"distrito": "Miraflores", "direccionExacta": "Av. Larco 1234"},
        contacto={"nombreCompleto": "Juan Pérez", "tipoDocumento": "DNI", "numeroDocumento": "72458912"},
    )
    assert req.guardarDireccion is True
    assert req.contacto.numeroDocumento == "72458912"

    for field in ("lineas", "subtotal", "descuento", "costoEnvio", "total", "precio"):
        with pytest.raises(ValidationError):
            CreateCheckoutRequest.model_validate({field: 1})

    session = CheckoutSessionResponse(
        id="chk-1",
        cartId="cart-1",
        pedidoId="PED-2026-00891",
        status="PENDING_PAYMENT",
        total=282.40,
        expiresAt="2026-11-05T18:15:00Z",
        intentosRestantes=3,
        summary={"total": 282.40},
    )
    assert session.pedidoId == "PED-2026-00891"

    pay_req = ProcessPaymentRequest(
        numeroTarjeta="4111111111111111",
        titular="JUAN PEREZ",
        vencimiento="12/28",
        cvv="123",
    )
    assert pay_req.cvv == "123"

    # Extra fields on payment payload must be forbidden
    with pytest.raises(ValidationError):
        ProcessPaymentRequest(
            numeroTarjeta="4111111111111111",
            titular="JUAN PEREZ",
            vencimiento="12/28",
            cvv="123",
            extraField="not-allowed",  # type: ignore[call-arg]
        )

    pay_res = PaymentResultResponse(
        id="att-1",
        checkoutId="chk-1",
        pedidoId="PED-2026-00891",
        marca="VISA",
        ultimos4="1111",
        monto=282.40,
        idTransaccion="SIM-123",
    )
    assert pay_res.resultado == "APROBADO"
    assert CHECKOUT_ERROR_STATUS["PAGO_RECHAZADO"] == 402
    assert CHECKOUT_ERROR_STATUS["CHECKOUT_EXPIRADO"] == 410
    assert CHECKOUT_ERROR_STATUS["CELULAR_NO_VERIFICADO"] == 403
    assert CHECKOUT_ERROR_STATUS["CARRITO_DESACTUALIZADO"] == 409
    assert SHIPPING_ERROR_STATUS["SIN_COBERTURA"] == 422
    assert ORDERS_ERROR_STATUS["LIMITE_REENVIOS_EXCEDIDO"] == 429


def test_checkout_request_accepts_frontend_payload_without_email() -> None:
    req = CreateCheckoutRequest.model_validate({
        "direccion": {"direccionExacta": "Av. Larco 1234", "distrito": "Miraflores", "referencia": "Frente al parque"},
        "contacto": {"nombreCompleto": "Juan Pérez", "tipoDocumento": "DNI", "numeroDocumento": "72458912"},
    })
    assert req.contacto.email is None
    assert req.contacto.model_dump(exclude_none=True) == {
        "nombreCompleto": "Juan Pérez",
        "tipoDocumento": "DNI",
        "numeroDocumento": "72458912",
    }


def test_checkout_contact_validation_uses_problem_json() -> None:
    app = FastAPI()
    register_problem_handlers(app)

    @app.post("/api/v1/checkout")
    def create_checkout(body: CreateCheckoutRequest) -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    response = client.post(
        "/api/v1/checkout",
        json={
            "contacto": {
                "nombreCompleto": "Juan Pérez",
                "tipoDocumento": "DNI",
                "numeroDocumento": "123",
            }
        },
    )
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "DATO_INVALIDO"

    amount_response = client.post("/api/v1/checkout", json={"total": 1})
    assert amount_response.status_code == 400
    assert amount_response.json()["code"] == "VALIDACION"


# ---------------------------------------------------------------------------
# 5. Orders, Status Mapping & Tracking Whitelist (`SPEC-16..18`)
# ---------------------------------------------------------------------------


def test_status_mapper_covers_sales_and_dispatch_matrix() -> None:
    assert map_order_status_label("CREADO") == "Pendiente de pago"
    assert map_order_status_label("PAGADO") == "Pago confirmado"
    assert map_order_status_label("EN_PREPARACION") == "En preparación"
    assert map_order_status_label("DESPACHADO", "PENDIENTE_ASIGNACION") == "En centro de despacho"
    assert (
        map_order_status_label(
            "DESPACHADO", "PENDIENTE_ASIGNACION", rescheduled_date="08/11"
        )
        == "Entrega reprogramada para 08/11"
    )
    assert map_order_status_label("DESPACHADO", "ASIGNADO") == "Asignado a repartidor"
    assert map_order_status_label("DESPACHADO", "EN_CAMINO") == "En camino"
    assert (
        map_order_status_label("DESPACHADO", "FALLIDO")
        == "No entregado, regresando al centro"
    )
    assert (
        map_order_status_label("DESPACHADO", "DEVUELTO_A_ORIGEN")
        == "Devuelto al centro de despacho (te contactaremos)"
    )
    assert (
        map_order_status_label("ENTREGADO", "EN_CAMINO", delivered_date="06/11")
        == "Entregado el 06/11"
    )
    assert map_order_status_label("ANULADO", "EN_CAMINO") == "Anulado"


def test_tracking_mapper_strips_forbidden_fields_and_llm_recipient() -> None:
    raw_from_dispatch = {
        "idPedido": "PED-2026-00891",
        "estado": "EN_CAMINO",
        "fechaProgramada": "2026-11-06",
        "distrito": "Miraflores",
        "recibidoPor": "Carlos Ruiz",
        "coordenadas": {"lat": -12.12, "lng": -77.03},
        "repartidor": {"nombre": "Pedro", "telefono": "999888777"},
        "direccionCompleta": "Av. Larco 1234 dpto 502",
        "motivoFallo": "Cliente ausente según nota interna",
        "hitos": [
            {
                "titulo": "En camino",
                "fecha": "2026-11-06T16:00:00-05:00",
                "completado": True,
                "comentarioRepartidor": "Tráfico pesado",
            }
        ],
    }

    ui_payload = sanitize_tracking_payload(raw_from_dispatch, for_llm=False)
    assert ui_payload["pedidoId"] == "PED-2026-00891"
    assert ui_payload["estadoEtiqueta"] == "En camino"
    assert ui_payload["recibidoPor"] == "Carlos Ruiz"
    assert "coordenadas" not in ui_payload
    assert "repartidor" not in ui_payload
    assert "direccionCompleta" not in ui_payload
    assert "motivoFallo" not in ui_payload
    assert "comentarioRepartidor" not in ui_payload["hitos"][0]

    llm_payload = sanitize_tracking_payload(raw_from_dispatch, for_llm=True)
    assert "recibidoPor" not in llm_payload


class _FakeOrdersService:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.resend_keys: list[str] = []

    async def list_orders(
        self,
        customer_id: Any,
        *,
        holder_token: str,
        estado: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, Any]:
        if self.error:
            raise self.error
        return {
            "items": [
                {
                    "pedidoId": "PED-2026-00891",
                    "canal": "CHATBOT",
                    "estado": "EN_PREPARACION",
                    "estadoEtiqueta": "En preparación",
                    "total": 282.40,
                    "moneda": "PEN",
                    "fechaCreacion": "2026-11-05T18:02:00Z",
                }
            ],
            "total": 1,
            "page": page,
            "pageSize": page_size,
        }

    async def get_order_detail(
        self, customer_id: Any, order_id: str, *, holder_token: str
    ) -> dict[str, Any]:
        if self.error:
            raise self.error
        return {
            "pedidoId": order_id,
            "canal": "CHATBOT",
            "estado": "DESPACHADO",
            "estadoEtiqueta": "En camino",
            "total": 282.40,
            "moneda": "PEN",
            "fechaCreacion": "2026-11-05T18:02:00Z",
            "items": [
                {
                    "productoId": "P-100",
                    "sku": "ZAP-RUN-42-NEG",
                    "descripcion": "Zapatilla X",
                    "cantidad": 1,
                    "precioUnitario": 299.90,
                }
            ],
            "historial": [
                {
                    "estado": "PAGADO",
                    "etiqueta": "Pago confirmado",
                    "fecha": "2026-11-05T18:02:00Z",
                    "completado": True,
                }
            ],
            "seguimientoDisponible": True,
        }

    async def get_order_tracking(
        self, customer_id: Any, order_id: str, *, holder_token: str
    ) -> dict[str, Any]:
        if self.error:
            raise self.error
        return {
            "estado": "EN_CAMINO",
            "distrito": "Miraflores",
            "coordenadas": {"lat": -12.1, "lng": -77.0},
            "repartidor": "Dato secreto",
            "hitos": [{"titulo": "En camino", "fecha": "2026-11-06T16:00:00-05:00"}],
        }

    async def resend_confirmation(
        self, customer_id: Any, order_id: str, *, holder_token: str, idempotency_key: str
    ) -> dict[str, Any]:
        if self.error:
            raise self.error
        self.resend_keys.append(idempotency_key)
        return {
            "ok": True,
            "pedidoId": order_id,
            "reenviosRestantes": 1,
            "correoEnmascarado": "j***z@example.com",
        }


def _build_orders_app(service: _FakeOrdersService, authenticated: bool = True) -> TestClient:
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(create_orders_router(lambda: service), prefix="/api/v1")
    app.dependency_overrides[get_caller_identity] = lambda: (
        CallerIdentity(customer_id=CUSTOMER_ID) if authenticated else None
    )
    return TestClient(app)


def test_orders_endpoints_contract_and_security_rules() -> None:
    service = _FakeOrdersService()
    client = _build_orders_app(service, authenticated=True)

    # 1. List orders
    r_list = client.get("/api/v1/pedidos", headers=AUTH_HEADER)
    assert r_list.status_code == 200
    assert r_list.json()["items"][0]["pedidoId"] == "PED-2026-00891"

    # 2. Order detail
    r_detail = client.get("/api/v1/pedidos/PED-2026-00891", headers=AUTH_HEADER)
    assert r_detail.status_code == 200
    assert r_detail.json()["seguimientoDisponible"] is True

    # 3. Tracking applies SPEC-18 privacy whitelist automatically
    r_track = client.get("/api/v1/pedidos/PED-2026-00891/seguimiento", headers=AUTH_HEADER)
    assert r_track.status_code == 200
    track_body = r_track.json()
    assert track_body["estadoEtiqueta"] == "En camino"
    assert "coordenadas" not in track_body
    assert "repartidor" not in track_body

    # 4. Foreign order (Ventas 403) masks as 404 RECURSO_NO_ENCONTRADO (SPEC-17 Req. 1)
    service.error = ModuleRequestError(
        module="sales", status=403, code="FORBIDDEN", detail="Not owner"
    )
    r_foreign = client.get("/api/v1/pedidos/PED-AJENO", headers=AUTH_HEADER)
    assert r_foreign.status_code == 404
    assert r_foreign.json()["code"] == "RECURSO_NO_ENCONTRADO"

    # 5. Resend requires an idempotency key and passes it to the service.
    service.error = None
    r_missing = client.post(
        "/api/v1/pedidos/PED-2026-00891/reenviar-confirmacion", headers=AUTH_HEADER
    )
    assert r_missing.status_code == 400
    assert r_missing.json()["code"] == "VALIDACION"
    assert service.resend_keys == []

    resend_headers = {**AUTH_HEADER, "Idempotency-Key": "resend-1"}
    r_resend = client.post(
        "/api/v1/pedidos/PED-2026-00891/reenviar-confirmacion", headers=resend_headers
    )
    assert r_resend.status_code == 202
    assert r_resend.json()["reenviosRestantes"] == 1
    assert service.resend_keys == ["resend-1"]

    service.error = OrderError(
        "LIMITE_REENVIOS_EXCEDIDO", "Ya alcanzaste el límite de 2 reenvíos para este pedido."
    )
    r_limit = client.post(
        "/api/v1/pedidos/PED-2026-00891/reenviar-confirmacion", headers=resend_headers
    )
    assert r_limit.status_code == 429
    assert r_limit.json()["code"] == "LIMITE_REENVIOS_EXCEDIDO"
