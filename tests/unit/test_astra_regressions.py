"""Follow-up adversarial checks. G6 shapes copied from OpenAPI 0.6.0-rc.3; no live G6 call."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.inbound.http.catalog_router import create_catalog_router
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.outbound.auth.service_token_provider import SecurityServiceTokenProvider
from src.adapters.outbound.http_clients.products_client import ProductsClient
from src.adapters.outbound.tools.registry_tool_adapter import RegistryToolAdapter
from src.application.dto.conversation_events import TokenEvent
from src.application.tools.catalog_handlers import build_catalog_handlers
from src.application.tools.catalog_tools import RecommendationArguments, SearchArguments, build_tool_registry
from src.domain.services.offer_policy import price_projection, promotion_summaries
from src.ports.outbound.llm_provider_port import TextDelta
from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError
from tests.unit.test_cart_use_case import CONVERSATION_ID, make_caller, make_cart_use_case, owned
from tests.unit.test_discovery import NOW, PRICE, PROMO, service
from tests.unit.test_interpret_and_respond_use_case import build, reply, run, tool_turn


def g6_detail(pid, brand, sizes=("42",)):
    """ProductoDetalleComercial with VarianteComercial rows."""
    return {
        "product_id": pid,
        "slug": pid,
        "name": f"Zapatilla {pid}",
        "description": "Para running",
        "categoria_id": "cat",
        "tipo_producto_id": "t",
        "marca_id": brand,
        "tiene_variantes": True,
        "images": [],
        "attributes": [],
        "marca": {"marca_id": brand, "name": brand.upper()},
        "variants": [
            {
                "variant_id": f"{pid}-{size}",
                "sku": f"{pid}-{size}",
                "identifying_attributes": [{"caracteristica_id": "c", "nombre": "Talla", "valor": size}],
                "non_identifying_attributes": [],
                "images": [],
            }
            for size in sizes
        ],
    }


def g6_service(details):
    uc, ctx = service()
    skus = [v["sku"] for d in details for v in d["variants"]]
    uc._catalog.search_products = AsyncMock(
        return_value={"items": [{"product_id": d["product_id"]} for d in details], "total": len(details)}
    )
    uc._catalog.get_product_detail = AsyncMock(
        side_effect=lambda pid: next(d for d in details if d["product_id"] == pid)
    )
    uc._catalog.get_prices = AsyncMock(
        return_value=[{**PRICE, "sku": s, "precio_regular": 100, "precio_oferta": None} for s in skus]
    )
    uc._inventory.get_availability = AsyncMock(return_value=[{"sku": s, "status": "DISPONIBLE"} for s in skus])
    return uc, ctx


def cards(result):
    return [p for b in result["bloques"] if b["tipo"] == "CARRUSEL_PRODUCTOS" for p in b["datos"]["productos"]]


@pytest.mark.parametrize("status,code", [(401, "TOKEN_INVALIDO"), (403, "PERMISO_DENEGADO")])
def test_g6_m2m_rejection_is_not_a_browser_auth_error(status, code):
    transport = httpx.MockTransport(lambda req: httpx.Response(status, json={"code": code}))
    client = ProductsClient(
        base_url="http://g6.test", client=httpx.AsyncClient(transport=transport, base_url="http://g6.test")
    )
    uc, _ = service()
    uc._catalog = client
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(create_catalog_router(lambda: uc), prefix="/api/v1")
    response = TestClient(app, raise_server_exceptions=False).get("/api/v1/catalogo/productos")
    assert response.status_code == 503, f"G3 answered {response.status_code} {response.text}"
    assert response.json()["code"] == "SERVICIO_NO_DISPONIBLE"


@pytest.mark.asyncio
async def test_g6_shape_size_filter_keeps_matching_size():
    uc, ctx = g6_service([g6_detail("p1", "m1", ("42", "43"))])
    result = await uc.recommend(RecommendationArguments(actividad="running", talla="42"), ctx)
    assert [p["product_id"] for p in cards(result)] == ["p1"], result["texto"]


@pytest.mark.asyncio
async def test_g6_shape_card_exposes_brand_and_size():
    uc, ctx = g6_service([g6_detail("p1", "m1", ("42",))])
    row = cards(await uc.recommend(RecommendationArguments(actividad="running"), ctx))[0]
    assert row["brand"] == "M1"
    assert row["variants"][0]["talla"] == "42", row["variants"]


@pytest.mark.asyncio
async def test_g6_shape_two_brand_diversity():
    details = [g6_detail(f"a{i}", "m1") for i in range(6)] + [g6_detail("b0", "m2")]
    uc, ctx = g6_service(details)
    rows = cards(await uc.recommend(RecommendationArguments(actividad="running"), ctx))
    assert "b0" in [r["product_id"] for r in rows], [r["product_id"] for r in rows]


@pytest.mark.asyncio
async def test_one_stale_product_does_not_fail_search():
    good = g6_detail("good", "m1")
    uc, ctx = g6_service([good])
    uc._catalog.search_products = AsyncMock(
        return_value={"items": [{"product_id": "gone"}, {"product_id": "good"}], "total": 2}
    )

    async def detail(pid):
        if pid == "gone":
            raise ModuleRequestError("productos", 404, "RECURSO_NO_ENCONTRADO")
        return good

    uc._catalog.get_product_detail = detail
    result = await uc.search(SearchArguments(q="x"), ctx)
    assert [p["product_id"] for p in cards(result)] == ["good"]


@pytest.mark.asyncio
async def test_refusal_is_not_forgotten_on_next_same_activity_call():
    uc, ctx = service()
    await uc.recommend(RecommendationArguments(actividad="futbol", omitirPreguntas=True), ctx)
    result = await uc.recommend(RecommendationArguments(actividad="futbol", nivel="principiante"), ctx)
    assert not any(b["tipo"] == "ACCIONES_RAPIDAS" for b in result["bloques"]), result["texto"]


@pytest.mark.asyncio
async def test_question_budget_survives_accent_variation():
    uc, ctx = service()
    asked = 0
    for activity in ("fútbol", "futbol", "fútbol"):
        result = await uc.recommend(RecommendationArguments(actividad=activity), ctx)
        asked += bool(result["bloques"]) and result["bloques"][0]["tipo"] == "ACCIONES_RAPIDAS"
    assert asked <= 2, f"{asked} questions asked for the same activity"


@pytest.mark.asyncio
@pytest.mark.parametrize("target_status,target_price", [("AGOTADO", 90), ("DISPONIBLE", 300)])
async def test_recommendation_size_must_meet_stock_and_budget(target_status, target_price):
    uc, ctx = service({"WANTED": target_status, "OTHER": "DISPONIBLE"})
    uc._catalog.search_products = AsyncMock(return_value={"items": [{"product_id": "p"}]})
    uc._catalog.get_product_detail = AsyncMock(
        return_value={
            "product_id": "p",
            "name": "Running shoe",
            "marca_id": "a",
            "description": "Running",
            "tiene_variantes": True,
            "variants": [
                {"sku": "WANTED", "identifying_attributes": [{"nombre": "Talla", "valor": "42"}]},
                {"sku": "OTHER", "identifying_attributes": [{"nombre": "Talla", "valor": "39"}]},
            ],
        }
    )
    uc._catalog.get_prices = AsyncMock(
        return_value=[
            {**PRICE, "sku": "WANTED", "precio_regular": target_price, "precio_oferta": None},
            {**PRICE, "sku": "OTHER", "precio_regular": 80, "precio_oferta": None},
        ]
    )
    result = await uc.recommend(RecommendationArguments(actividad="running", talla="42", presupuestoMax=100), ctx)
    products = [p for b in result["bloques"] if b["tipo"] == "CARRUSEL_PRODUCTOS" for p in b["datos"]["productos"]]
    assert products == [], f"Recommended incompatible requested size: {products}"


@pytest.mark.asyncio
@pytest.mark.parametrize("product_id", [None, "prod-001"])
async def test_frontend_sku_only_add_supplies_real_cross_sell_source(product_id):
    cart, convs = make_cart_use_case()
    owned(cart, convs)
    cart._discovery = AsyncMock()
    cart._discovery.cross_sell.return_value = []
    await cart.add(make_caller(), CONVERSATION_ID, sku="NIK-REV7-42-NEG", product_id=product_id)
    source = cart._discovery.cross_sell.call_args.args[0]
    assert source == "prod-001"
    assert (await cart.view(make_caller(), CONVERSATION_ID))["bloques"][0]["datos"]["lineas"][0]["nombre"] == "Nike Revolution 7"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evaluation",
    [
        {"currency": "PEN", "result_amount": "1.00"},
        {"currency": "PEN", "original_amount": "329.90", "result_amount": "1.00"},
        {"original_amount": "329.90", "discount_amount": "0.00", "result_amount": "329.90"},
    ],
)
async def test_partial_provider_evaluation_must_not_be_accepted(evaluation):
    cart, convs = make_cart_use_case()
    owned(cart, convs)
    cart._catalog.evaluate_promotions = AsyncMock(return_value=evaluation)
    result = await cart.add(make_caller(), CONVERSATION_ID, sku="NIK-REV7-42-NEG")
    data = result["bloques"][0]["datos"]
    assert data["promociones_ok"] is False, f"Malformed evaluation accepted: {data}"


@pytest.mark.asyncio
async def test_streamed_safe_recommendation_matches_persisted_text():
    discovery, _ = service()
    adapter = RegistryToolAdapter(build_tool_registry(build_catalog_handlers(discovery)))
    engine, conv, messages, _, _, _ = build(
        [tool_turn("need", "recomendar_productos", {"actividad": "running"}), reply("Todas impermeables.")],
        tools=adapter,
    )
    discovery._conversations = conv
    events = await run(engine, conv)
    streamed = "".join(e.text for e in events if isinstance(e, TokenEvent))
    final = events[-1].message.text
    assert streamed == final, f"live={streamed!r}; persisted={final!r}"


@pytest.mark.asyncio
async def test_pretool_invented_recommendation_never_leaks_to_stream():
    discovery, _ = service()
    adapter = RegistryToolAdapter(build_tool_registry(build_catalog_handlers(discovery)))
    first = [
        TextDelta("Todas las opciones son impermeables. "),
        *tool_turn("need", "recomendar_productos", {"actividad": "running"}),
    ]
    engine, conv, _, _, _, _ = build([first, reply("Listo.")], tools=adapter)
    discovery._conversations = conv
    events = await run(engine, conv)
    assert "impermeables" not in "".join(e.text for e in events if isinstance(e, TokenEvent))


@pytest.mark.parametrize("kind", ["price", "promotion"])
def test_empty_expiry_not_promoted_to_indefinite_validity(kind):
    if kind == "price":
        try:
            result = price_projection({**PRICE, "valid_until": ""}, NOW)
        except ValueError:
            return
        assert result["offer_price"] is None, result
    else:
        with pytest.raises(ValueError):
            promotion_summaries([{**PROMO, "valid_until": ""}], NOW)


@pytest.mark.asyncio
async def test_rejected_service_token_is_invalidated_and_refreshed():
    issued = []

    def issue(request):
        issued.append(request)
        return httpx.Response(200, json={"accessToken": f"service-{len(issued)}", "expiresIn": 900})

    async with httpx.AsyncClient(transport=httpx.MockTransport(issue), base_url="https://g7.test") as security:
        provider = SecurityServiceTokenProvider("https://g7.test", "test-client", "fake-secret", client=security)
        assert await provider.get_token() == "service-1"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"code": "TOKEN_INVALIDO"})),
            base_url="https://g6.test",
        ) as upstream:
            client = ProductsClient("https://g6.test", client=upstream, service_token_provider=provider)
            with pytest.raises(ModuleUnavailableError):
                await client.search_products()
        assert await provider.get_token() == "service-2"
        provider.invalidate("service-1")
        assert await provider.get_token() == "service-2"
        assert len(issued) == 2


@pytest.mark.asyncio
async def test_matching_size_supplies_the_displayed_price():
    detail = g6_detail("shoe", "m1", ("39", "42"))
    uc, ctx = g6_service([detail])
    uc._catalog.get_prices = AsyncMock(return_value=[
        {**PRICE, "sku": "shoe-39", "precio_regular": 80, "precio_oferta": None},
        {**PRICE, "sku": "shoe-42", "precio_regular": 95, "precio_oferta": None},
    ])
    rows = cards(await uc.recommend(RecommendationArguments(actividad="running", talla="42"), ctx))
    assert rows[0]["price"] == 95
    assert rows[0]["price_text"] == "S/ 95.00"
    assert rows[0]["single_sku"] == "shoe-42"


def test_rest_add_does_not_consume_cross_sell():
    from tests.unit.test_catalog_cart_routers import CONVERSATION_ID as cid
    from tests.unit.test_catalog_cart_routers import make_app

    client, cart = make_app()
    cart._discovery = AsyncMock()
    response = client.post("/api/v1/carrito/items", params={"conversacionId": str(cid)},
                           json={"sku": "NIK-REV7-42-NEG", "product_id": "prod-001"})
    assert response.status_code == 200
    cart._discovery.cross_sell.assert_not_called()


@pytest.mark.asyncio
async def test_unresolved_bare_sku_skips_cross_sell():
    cart, convs = make_cart_use_case()
    owned(cart, convs)
    cart._catalog.search_products = AsyncMock(return_value={"items": []})
    cart._discovery = AsyncMock()
    result = await cart.add(make_caller(), CONVERSATION_ID, sku="NIK-REV7-42-NEG")
    assert result["code"] == "OK"
    cart._discovery.cross_sell.assert_not_called()
