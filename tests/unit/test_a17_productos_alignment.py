"""A17 compatibility closure: Productos service token, scopes and D2 semantics.

Every test cites the spec scenario it protects. HTTP evidence here comes from
`respx` against the published OpenAPI; it never claims live G6 validation.
"""
import json
import uuid

import httpx
import pytest
import respx

from src.adapters.inbound.http.cart_router import _problem
from src.application.use_cases.manage_cart_use_case import CartError, ManageCartUseCase
from src.modules.mod_catalog.adapters.fake_products import FakeCatalogPort, FakeInventoryPort
from src.modules.mod_catalog.adapters.inventory_client import InventoryClient
from src.modules.mod_catalog.adapters.products_client import (
    PRODUCTS_AUDIENCE,
    ProductsClient,
)
from src.modules.mod_catalog.domain.stock_validator import StockValidator
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError
from src.shared.domain.request_context import correlation_id_var
from tests.fakes.fake_commerce import FakeCarts, FakeConversations, FakeItems, FakeMessages, FakeUow

BASE = "http://g6.test"

CATALOG_SCOPE = "catalogo:leer"
PRICES_SCOPE = "precios:leer"
PROMOTIONS_SCOPE = "promociones:leer"
PROMOTIONS_EVALUATE_SCOPE = "promociones:evaluar"
RECOMMENDATIONS_SCOPE = "recomendaciones:leer"
INVENTORY_SCOPE = "inventario:disponibilidad:leer"


class RecordingTokenProvider:
    def __init__(self, token: str = "service-token"):
        self.token = token
        self.calls: list[dict] = []

    async def get_token(self, *, correlation_id=None, scope=None, audience=None):
        self.calls.append({"correlation_id": correlation_id, "scope": scope, "audience": audience})
        return self.token

    def invalidate(self, rejected_token=None):
        pass


def make_products(provider=None):
    return ProductsClient(BASE, service_token_provider=provider) if provider else ProductsClient(BASE)


def make_inventory(provider=None):
    return InventoryClient(BASE, service_token_provider=provider) if provider else InventoryClient(BASE)


def cart_with(sku: str, statuses: dict | None, *, price: str | None = "99.90"):
    product = {
        "product_id": "prod-a", "name": "Runner A", "category_id": "cat-zapatillas",
        "sku_base": sku, "precio_regular": 99.9, "precio_oferta": 99.9,
    }
    prices = {sku: {"sku": sku, "precio_regular": price, "precio_oferta": None,
                    "currency": "PEN", "valid_from": "2026-01-01T00:00:00Z",
                    "valid_until": None}}
    conversations = FakeConversations()
    conversation_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    conversations.add_conversation(conversation_id, customer_id=customer_id, context={})
    cart = ManageCartUseCase(
        FakeCarts(), FakeItems(),
        FakeCatalogPort(products=[product], details={}, prices=prices),
        FakeInventoryPort(statuses or {}), conversations, FakeMessages(), FakeUow(),
    )
    return cart, conversations, CallerIdentity(customer_id=customer_id), conversation_id


async def add_line(cart, conversations, caller, conversation_id, sku):
    return await cart.add(caller, conversation_id, sku=sku, quantity=1)


# -- SPEC-10 · Req. 1 · Contexto de cumplimiento de la verificación -----------

@pytest.mark.asyncio
@respx.mock
async def test_spec_10_req1_sends_fulfillment_without_ref_and_reads_result():
    """SPEC-10 Req. 1 · Scenario: Contexto de cumplimiento de la verificación."""
    route = respx.post(f"{BASE}/inventario/disponibilidad/comercial/verificar").mock(
        return_value=httpx.Response(200, json={
            "evaluated_at": "2026-11-05T00:00:00Z", "binding": False,
            "items": [{"sku": "SKU-A", "requested_quantity": 2, "result": "SUFICIENTE",
                       "reason_code": None}],
        })
    )
    rows = await make_inventory().verify_quantities([{"sku": "SKU-A", "quantity": 2}])
    body = json.loads(route.calls[0].request.content)
    assert body["fulfillment"] == {"mode": "ENVIO_DOMICILIO"}
    assert "fulfillment_ref" not in body
    assert rows == [{"sku": "SKU-A", "requested_quantity": 2, "result": "SUFICIENTE",
                     "reason_code": None}]
    assert StockValidator().check_quantity(rows[0]["result"]).ok is True


@pytest.mark.asyncio
async def test_spec_10_req1_indeterminate_is_accepted_with_warning_never_few_units():
    """SPEC-10 Req. 1 · Scenario: Disponibilidad indeterminada."""
    cart, conversations, caller, conversation_id = cart_with("SKU-A", {"SKU-A": "INDETERMINADO"})
    result = await add_line(cart, conversations, caller, conversation_id, "SKU-A")
    assert result["code"] == "OK"
    assert "No pudimos confirmar el stock en este momento" in result["texto"]
    assert "pocas unidades" not in result["texto"].casefold()
    assert result["bloques"][0]["datos"]["lineas"][0]["sku"] == "SKU-A"


# -- SPEC-10 · Req. 3 · Respuesta incompleta o incompatible --------------------

@pytest.mark.parametrize("rows, expected", [
    ([], []),                                              # missing requested SKU
    ([{"sku": "OTHER", "status": "DISPONIBLE"}], []),      # extra SKU only
    ([{"sku": "SKU-A", "status": "DESCONOCIDO"}], []),     # unpublished status
    ([{"sku": "SKU-A"}], []),                              # incomplete row
    ([None, {"sku": "SKU-A", "status": "DISPONIBLE"},      # valid neighbour survives,
      {"sku": "SKU-A", "status": "AGOTADO"}],             # duplicate is ignored
     [{"sku": "SKU-A", "status": "DISPONIBLE"}]),
])
@pytest.mark.asyncio
async def test_spec_10_req3_incomplete_or_incompatible_payload_is_not_obtained(rows, expected):
    """SPEC-10 Req. 3 · Scenario: Respuesta incompleta o incompatible."""
    async with httpx.AsyncClient(
        base_url=BASE,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"items": rows})),
    ) as client:
        result = await InventoryClient(BASE, client=client).get_availability(["SKU-A"])
    assert result == expected
    if not expected:
        assert StockValidator().check_row(None).code == "SERVICIO_NO_DISPONIBLE"


@pytest.mark.asyncio
async def test_spec_10_req3_missing_availability_rejects_cart_and_has_no_badge():
    """SPEC-10 Req. 3 · Scenario: Respuesta incompleta o incompatible (cart + card)."""
    cart, conversations, caller, conversation_id = cart_with("SKU-A", {})
    with pytest.raises(CartError) as error:
        await add_line(cart, conversations, caller, conversation_id, "SKU-A")
    assert error.value.code == "SERVICIO_NO_DISPONIBLE"
    assert error.value.code not in {"SKU_AGOTADO", "INDETERMINADO"}


# -- SPEC-10 · Req. 3 · Error de autorización o límite de frecuencia -----------

@pytest.mark.parametrize("status", [401, 403])
@respx.mock
@pytest.mark.asyncio
async def test_spec_10_req3_auth_errors_are_unavailability_not_commercial_states(status):
    """SPEC-10 Req. 3 · Scenario: Error de autorización o límite de frecuencia."""
    provider = RecordingTokenProvider()
    respx.route(method="GET", url=f"{BASE}/inventario/disponibilidad/comercial").mock(
        return_value=httpx.Response(status, json={"code": "SCOPE_INSUFICIENTE"})
    )
    with pytest.raises(ModuleUnavailableError):
        await make_inventory(provider).get_availability(["SKU-A"])


@pytest.mark.asyncio
@respx.mock
async def test_spec_10_req3_rate_limit_keeps_http_semantics_and_retry_after():
    """SPEC-10 Req. 3 · Scenario: Error de autorización o límite de frecuencia."""
    respx.post(f"{BASE}/inventario/disponibilidad/comercial/verificar").mock(
        return_value=httpx.Response(
            429, json={"code": "DEMASIADAS_SOLICITUDES"}, headers={"Retry-After": "30"}
        )
    )
    with pytest.raises(ModuleRequestError) as error:
        await make_inventory().verify_quantities([{"sku": "SKU-A", "quantity": 1}])
    assert error.value.status == 429 and error.value.retry_after == "30"
    cart_error = ManageCartUseCase._map_module_error(error.value)
    assert cart_error.code == "DEMASIADAS_SOLICITUDES"
    problem = _problem(cart_error)
    assert problem.status == 429
    assert ("Retry-After", "30") in problem.headers


@pytest.mark.parametrize("status", [401, 403, 422])
def test_spec_10_req3_auth_and_invalid_payload_never_become_stock_states(status):
    """SPEC-10 Req. 3: 401/403/422 never turn into AGOTADO or INDETERMINADO."""
    cart_error = ManageCartUseCase._map_module_error(
        ModuleRequestError("productos", status, "UPSTREAM")
    )
    assert cart_error.code == "SERVICIO_NO_DISPONIBLE"
    assert _problem(cart_error).status == 503


# -- SPEC-09 · Req. 1 · Precio no disponible -----------------------------------

@pytest.mark.asyncio
async def test_spec_09_req1_missing_price_is_null_and_cart_rejects_the_sku():
    """SPEC-09 Req. 1 · Scenario: Precio no disponible."""
    cart, conversations, caller, conversation_id = cart_with("SKU-A", {"SKU-A": "DISPONIBLE"},
                                                             price=None)
    with pytest.raises(CartError) as error:
        await add_line(cart, conversations, caller, conversation_id, "SKU-A")
    assert error.value.code == "PRODUCTO_NO_DISPONIBLE"
    assert error.value.detail == "Precio no disponible"


# -- SPEC-10 · Req. 4 · Disponibilidad que no se puede confirmar ---------------

@pytest.mark.asyncio
async def test_spec_10_req4_indeterminate_answers_without_claiming_stock():
    """SPEC-10 Req. 4 · Scenario: Disponibilidad que no se puede confirmar."""
    validator = StockValidator()
    check = validator.check_row({"sku": "SKU-A", "status": "INDETERMINADO"})
    assert check.ok is True and check.code == "INDETERMINADO"
    assert check.message == (
        "No pudimos confirmar el stock en este momento; lo verificaremos antes del pago."
    )
    assert "pocas" not in check.message.casefold()


# -- A17: token, audience, scope, correlation, channel, no `estado` ------------

@pytest.mark.asyncio
@respx.mock
async def test_a17_every_operation_asks_the_operation_scope_for_api_productos():
    provider = RecordingTokenProvider()
    catalog, inventory = make_products(provider), make_inventory(provider)
    respx.get(f"{BASE}/productos").mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get(f"{BASE}/productos/p1").mock(return_value=httpx.Response(200, json={
        "product_id": "p1", "variants": []}))
    respx.get(f"{BASE}/categorias").mock(return_value=httpx.Response(200, json={"categories": []}))
    respx.get(f"{BASE}/marcas").mock(return_value=httpx.Response(200, json={"brands": []}))
    respx.get(f"{BASE}/precios/skus/S").mock(return_value=httpx.Response(200, json={"sku": "S"}))
    respx.get(f"{BASE}/precios").mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get(f"{BASE}/promociones").mock(return_value=httpx.Response(200, json={"items": []}))
    respx.post(f"{BASE}/promociones/evaluar").mock(
        return_value=httpx.Response(200, json={"discount_amount": 0}))
    respx.get(f"{BASE}/recomendaciones").mock(
        return_value=httpx.Response(200, json={"recommendations": []}))
    respx.get(f"{BASE}/inventario/disponibilidad/comercial").mock(
        return_value=httpx.Response(200, json={"items": []}))
    respx.post(f"{BASE}/inventario/disponibilidad/comercial/verificar").mock(
        return_value=httpx.Response(200, json={"items": []}))

    await catalog.search_products()
    await catalog.get_product_detail("p1")
    await catalog.list_categories()
    await catalog.list_brands()
    await catalog.get_price("S")
    await catalog.get_prices(["S"])
    await catalog.list_promotions()
    await catalog.evaluate_promotions([{"sku": "S", "quantity": 1}])
    await catalog.get_recommendations("p1")
    await inventory.get_availability(["S"])
    await inventory.verify_quantities([{"sku": "S", "quantity": 1}])

    assert [call["scope"] for call in provider.calls] == [
        CATALOG_SCOPE, CATALOG_SCOPE, CATALOG_SCOPE, CATALOG_SCOPE,
        PRICES_SCOPE, PRICES_SCOPE, PROMOTIONS_SCOPE, PROMOTIONS_EVALUATE_SCOPE,
        RECOMMENDATIONS_SCOPE, INVENTORY_SCOPE, INVENTORY_SCOPE,
    ]
    assert all(call["audience"] == PRODUCTS_AUDIENCE for call in provider.calls)
    assert all(route.calls.last.request.headers["Authorization"] == "Bearer service-token"
               for route in respx.routes if route.called)


@pytest.mark.asyncio
@respx.mock
async def test_a17_one_uuid_correlation_per_turn_channel_present_and_no_estado():
    provider = RecordingTokenProvider()
    catalog = make_products(provider)
    turn_id = str(uuid.uuid4())
    respx.get(f"{BASE}/productos").mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get(f"{BASE}/productos/p1").mock(return_value=httpx.Response(200, json={
        "product_id": "p1", "variants": []}))
    respx.get(f"{BASE}/precios/skus/S").mock(return_value=httpx.Response(200, json={"sku": "S"}))
    respx.get(f"{BASE}/promociones").mock(return_value=httpx.Response(200, json={"items": []}))

    token = correlation_id_var.set(turn_id)
    try:
        await catalog.search_products(q="zapatillas")
        await catalog.get_product_detail("p1")
        await catalog.get_price("S")
        await catalog.list_promotions()
    finally:
        correlation_id_var.reset(token)

    requests = [route.calls.last.request for route in respx.routes if route.called]
    assert requests
    assert all(request.headers["X-Correlation-Id"] == turn_id for request in requests)
    assert uuid.UUID(turn_id).version == 4
    search = next(request for request in requests if request.url.path.endswith("/productos"))
    assert "estado" not in search.url.params
    assert search.url.params["canal"] == "CHATBOT"
    detail = next(request for request in requests if request.url.path.endswith("/productos/p1"))
    assert detail.url.params["canal"] == "CHATBOT"
    price = next(request for request in requests if "/precios/" in request.url.path)
    assert price.url.params["canal"] == "CHATBOT"
    promotions = next(request for request in requests if request.url.path.endswith("/promociones"))
    assert promotions.url.params["canal"] == "CHATBOT"
    assert promotions.url.params["vigentes"] == "true"
    assert all(call["correlation_id"] == turn_id for call in provider.calls)


def test_a17_fake_inventory_never_invents_availability():
    inventory = FakeInventoryPort()
    assert inventory._status("NOT-IN-SEEDS") is None
    assert StockValidator().check_row(None).code == "SERVICIO_NO_DISPONIBLE"
    assert StockValidator().check_row(None).code != "INDETERMINADO"
