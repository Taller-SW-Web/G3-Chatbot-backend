"""H4-F6 behavioral contracts, fake persistence and real HTTP request assertions."""
import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.adapters.inbound.http.catalog_router import create_catalog_router
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.outbound.auth.service_token_provider import SecurityServiceTokenProvider
from src.adapters.outbound.fakes.fake_products import FakeCatalogPort, FakeInventoryPort
from src.adapters.outbound.http_clients.inventory_client import InventoryClient
from src.adapters.outbound.http_clients.products_client import ProductsClient
from src.application.tools.action_dispatcher import ActionDispatcher
from src.application.tools.catalog_handlers import build_catalog_handlers
from src.application.tools.catalog_tools import (
    EmptyArguments,
    ProductArguments,
    PromotionArguments,
    RecommendationArguments,
    SearchArguments,
    build_tool_registry,
)
from src.application.tools.tool_registry import ExecutionContext
from src.application.use_cases.manage_catalog_use_case import CatalogError, ManageCatalogUseCase
from src.domain.services.offer_policy import price_projection, promotion_summaries
from src.ports.outbound.module_errors import ModuleUnavailableError
from tests.fakes.fake_commerce import FakeConversations, FakeUow

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
PROMO = {"promotion_id": "sale", "name": "Campaña", "status": "ACTIVA",
         "valid_from": "2026-01-01T00:00:00Z", "valid_until": None, "channels": []}
PRICE = {"precio_regular": "299.90", "precio_oferta": "239.90", "currency": "PEN",
         "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z"}


def service(statuses=None):
    products = [{"product_id": str(i), "name": f"Running {i}", "category_id": "cat-zapatillas",
                 "marca_id": "a" if i < 6 else "b", "marca": {"name": "Marca A" if i < 6 else "Marca B"}, "sku_base": f"SKU{i}", "precio_regular": 100 + i}
                for i in range(8)]
    catalog = FakeCatalogPort(products=products, details={}, prices={
        f"SKU{i}-STD": {**PRICE, "sku": f"SKU{i}-STD", "precio_regular": 100 + i,
                       "precio_oferta": 80 + i} for i in range(8)})
    conversations = FakeConversations()
    cid = uuid.uuid4()
    conversations.add_conversation(cid, anonymous_sid="owner")
    context = ExecutionContext(conversacion_id=str(cid), chat_sid="owner")
    uc = ManageCatalogUseCase(catalog, FakeInventoryPort(statuses), conversations, FakeUow())
    return uc, context


def test_offer_rounding_expiry_and_missing_price():
    result = price_projection(PRICE, NOW)
    assert result["price"] == Decimal("239.90") and result["savings_pct"] == 20
    assert price_projection({**PRICE, "valid_until": "2026-02-01T00:00:00Z"}, NOW)["offer_price"] is None
    assert price_projection({})["price"] is None
    assert price_projection({**PRICE, "valid_from": None})["offer_price"] is None


@pytest.mark.parametrize("change", [{"currency": "USD"}, {"precio_regular": "NaN"},
    {"precio_oferta": -1}, {"valid_until": "invalid"}, {"valid_from": "2026-01-01"}])
def test_invalid_prices_rejected(change):
    with pytest.raises(ValueError):
        price_projection({**PRICE, **change}, NOW)


def test_promotions_only_active_current_channel_and_safe_fields():
    rows = [{**PROMO, "coupon_code": "SECRET", "benefit": "invented"},
            {**PROMO, "channels": ["RETAIL"]}, {**PROMO, "status": "INACTIVA"},
            {**PROMO, "valid_until": "2026-02-01T00:00:00Z"}]
    summaries = promotion_summaries(rows, NOW)
    assert len(summaries) == 1
    assert set(summaries[0]) == {"promotion_id", "name", "valid_from", "valid_until"}
    assert "SECRET" not in str(summaries)
    assert promotion_summaries([], NOW) == []


@pytest.mark.parametrize("budget", [-1, float("inf"), float("nan")])
def test_recommendation_schema_rejects_bad_budget(budget):
    with pytest.raises(ValidationError):
        RecommendationArguments(actividad="running", presupuestoMax=budget)


@pytest.mark.asyncio
async def test_recommendations_budget_diversity_exclusion_and_ordinals():
    uc, ctx = service({"SKU0-STD": "AGOTADO", "SKU1-STD": "INDETERMINADO"})
    result = await uc.recommend(RecommendationArguments(actividad="running", presupuestoMax=100), ctx)
    rows = result["bloques"][0]["datos"]["productos"]
    assert len(rows) == 5 and len({row["brand"] for row in rows}) == 2
    assert all(row["price"] <= 100 and row["product_id"] != "0" for row in rows)
    assert all(row["recommendation_reason"] == f"{row['short_description']} · {row['price_text']}" for row in rows)
    memory = await uc._read_memory(uuid.UUID(ctx.conversacion_id))
    assert memory["ultimoCarrusel"] == [row["product_id"] for row in rows]
    detail = await uc.detail(ProductArguments(productoRef=1), ctx)
    assert detail["bloques"][0]["datos"]["producto"]["product_id"] == rows[0]["product_id"]


@pytest.mark.asyncio
async def test_clarifications_persist_two_max_and_refusal_chip():
    uc, ctx = service()
    args = RecommendationArguments(actividad="fútbol", destinatario="hijo")
    for count in (1, 2):
        result = await uc.recommend(args, ctx)
        actions = result["bloques"][0]["datos"]["acciones"]
        assert actions[-1]["payload"]["omitirPreguntas"]
        assert (await uc._read_memory(uuid.UUID(ctx.conversacion_id)))["recommendationQuestions"] == count
    assert (await uc.recommend(args, ctx))["bloques"] == []


@pytest.mark.asyncio
async def test_missing_availability_fails_not_commercial_empty():
    uc, ctx = service()
    uc._inventory.get_availability = AsyncMock(return_value=[])
    with pytest.raises(CatalogError):
        await uc.recommend(RecommendationArguments(actividad="running"), ctx)


@pytest.mark.asyncio
async def test_cross_sell_persistence_once_silence_resume_and_no_leakage():
    uc, ctx = service({"SKU1-STD": "INDETERMINADO", "SKU2-STD": "AGOTADO"})
    rows = await uc.cross_sell("0", ctx)
    assert len(rows[0]["datos"]["productos"]) == 1
    reloaded = ManageCatalogUseCase(uc._catalog, uc._inventory, uc._conversations, uc._uow)
    assert await reloaded.cross_sell("0", ctx) == []
    await reloaded.silence(EmptyArguments(), ctx)
    assert await reloaded.cross_sell("4", ctx) == []
    resumed = await reloaded.recommend(RecommendationArguments(actividad="complementos"), ctx)
    assert resumed["bloques"]
    other = uuid.uuid4()
    uc._conversations.add_conversation(other, anonymous_sid="second")
    assert await uc.cross_sell("0", ExecutionContext(conversacion_id=str(other), chat_sid="second"))
    with pytest.raises(CatalogError):
        await uc.cross_sell("0", ExecutionContext(conversacion_id=str(other), chat_sid="owner"))


@pytest.mark.asyncio
async def test_direct_actions_share_tools_and_promotion_errors_are_not_empty():
    uc, ctx = service()
    registry = build_tool_registry(build_catalog_handlers(uc))
    dispatcher = ActionDispatcher(registry)
    result = await dispatcher.despachar({"tipo": "RECOMENDAR_PRODUCTOS", "payload": {"actividad": "running"}}, ctx)
    assert result.code == "OK" and result.data["bloques"][0]["datos"]["variant"] == "recomendacion"
    uc._catalog.list_promotions = AsyncMock(side_effect=ModuleUnavailableError("productos"))
    result = await dispatcher.despachar({"tipo": "VER_OFERTAS", "payload": {}}, ctx)
    assert result.code == "SERVICIO_NO_DISPONIBLE"


def test_anonymous_rest_promotions_and_offer_page_scope():
    uc, _ = service()
    uc._catalog.list_promotions = AsyncMock(return_value=[PROMO])
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(create_catalog_router(lambda: uc))
    with TestClient(app) as client:
        assert client.get("/catalogo/promociones").json()["items"][0]["name"] == "Campaña"
        response = client.get("/catalogo/productos?soloOfertas=true")
        assert response.status_code == 200
        body = response.json()
        assert body["total_scope"] == "page" and body["total"] == len(body["items"])


@pytest.mark.asyncio
async def test_actual_http_bearer_canal_candidates_and_retry_after():
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/recomendaciones"):
            return httpx.Response(200, json={"recommendations": []})
        if request.url.path.endswith("/productos"):
            return httpx.Response(429, json={"code": "RATE_LIMIT"}, headers={"Retry-After": "12"})
        return httpx.Response(200, json={"items": []})
    client = httpx.AsyncClient(base_url="https://g6/api/v1/", transport=httpx.MockTransport(respond))
    provider = AsyncMock()
    provider.get_token.return_value = "service-token"
    products = ProductsClient("https://g6/api/v1", client=client, service_token_provider=provider)
    inventory = InventoryClient("https://g6/api/v1", client=client, service_token_provider=provider)
    await products.list_promotions(correlation_id=str(uuid.uuid4()))
    await products.get_recommendations("product with spaces")
    await inventory.get_availability(["A"])
    assert all(request.headers["Authorization"] == "Bearer service-token" for request in requests)
    assert dict(requests[0].url.params) == {"canal": "CHATBOT", "vigentes": "true"}
    assert dict(requests[1].url.params) == {"productoId": "product with spaces", "canal": "CHATBOT"}
    from src.ports.outbound.module_errors import ModuleRequestError
    with pytest.raises(ModuleRequestError) as error:
        await products.search_products()
    assert error.value.retry_after == "12" and "estado" not in requests[-1].url.params
    await client.aclose()


@pytest.mark.asyncio
async def test_shared_token_provider_single_refresh_under_concurrency():
    requests = []
    async def respond(request):
        requests.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, json={"accessToken": "service", "expiresIn": 900})
    async with httpx.AsyncClient(base_url="https://g7", transport=httpx.MockTransport(respond)) as client:
        provider = SecurityServiceTokenProvider("https://g7", "modulo-chatbot", "test-secret", client=client)
        assert await asyncio.gather(*(provider.get_token() for _ in range(5))) == ["service"] * 5
        assert len(requests) == 1 and b"grant_type=client_credentials" in requests[0].content


@pytest.mark.asyncio
async def test_offers_filter_products_but_not_promotion_list_and_cache():
    uc, ctx = service()
    uc._catalog.list_promotions = AsyncMock(return_value=[PROMO])
    result = await uc.offers(PromotionArguments(marca="Nike"), ctx)
    assert result["bloques"][0]["datos"]["promociones"][0]["name"] == "Campaña"
    await uc.promotions()
    assert uc._catalog.list_promotions.await_count == 1


@pytest.mark.asyncio
async def test_successful_cart_add_survives_merchandising_failure():
    from tests.unit.test_cart_use_case import CONVERSATION_ID, make_caller, make_cart_use_case, owned
    cart, conversations = make_cart_use_case()
    owned(cart, conversations)
    cart._discovery = AsyncMock()
    cart._discovery.cross_sell.side_effect = ModuleUnavailableError("productos")
    result = await cart.add(make_caller(), CONVERSATION_ID, sku="NIK-REV7-42-NEG")
    assert result["code"] == "OK"
    assert (await cart.view(make_caller(), CONVERSATION_ID))["bloques"][0]["datos"]["lineas"][0]["cantidad"] == 1
    assert cart._uow.commits > 0


@pytest.mark.asyncio
async def test_cart_expired_offer_notice_and_authoritative_evaluation():
    from tests.unit.test_cart_use_case import CONVERSATION_ID, make_caller, make_cart_use_case, owned
    cart, conversations = make_cart_use_case()
    owned(cart, conversations)
    cart._catalog._prices["NIK-REV7-42-NEG"] = {**PRICE, "sku": "NIK-REV7-42-NEG"}
    await cart.add(make_caller(), CONVERSATION_ID, sku="NIK-REV7-42-NEG")
    memory = await conversations.get_context(CONVERSATION_ID)
    memory["cartOfferPrices"]["NIK-REV7-42-NEG"]["offer_valid_until"] = "2026-01-02T00:00:00Z"
    await conversations.save_context(CONVERSATION_ID, memory)
    cart._catalog._prices["NIK-REV7-42-NEG"]["valid_until"] = "2026-01-02T00:00:00Z"
    cart._catalog.evaluate_promotions = AsyncMock(return_value={"original_amount": "299.90",
        "discount_amount": "10.00", "result_amount": "289.90", "currency": "PEN", "reason": "Campaña aplicada"})
    result = await cart.view(make_caller(), CONVERSATION_ID)
    data = result["bloques"][0]["datos"]
    assert data["total"] == 289.9 and data["promotion_reason"] == "Campaña aplicada"
    assert any("La oferta de" in notice and "299.90" in notice for notice in data["avisos"])
    assert data["descuentos"][0]["nombre"] == "Descuento"


@pytest.mark.asyncio
async def test_streamed_recommendations_reject_model_factual_justification():
    from src.adapters.outbound.tools.registry_tool_adapter import RegistryToolAdapter
    from tests.unit.test_interpret_and_respond_use_case import build, reply, run, tool_turn
    discovery, _ = service()
    adapter = RegistryToolAdapter(build_tool_registry(build_catalog_handlers(discovery)))
    engine, conversation, messages, _, _, _ = build([
        tool_turn("need", "recomendar_productos", {"actividad": "running"}),
        reply("Todas son impermeables y tienen suela de carbono.")], tools=adapter)
    discovery._conversations = conversation
    events = await run(engine, conversation)
    final = events[-1].message
    assert "impermeables" not in str([getattr(event, "text", "") for event in events])
    assert "impermeables" not in final.text and "carbono" not in final.text
    assert "Running" in final.text
    assert final.blocks["blocks"][0]["datos"]["variant"] == "recomendacion"
    assert messages.rows[-1]["text"] == final.text


@pytest.mark.asyncio
@pytest.mark.parametrize("pending,text,expected", [
    (True, "no, gracias", "silenciar_complementos"),
    (False, "no, gracias", None),
    (True, "no quiero ese color", None),
])
async def test_stream_refusal_only_silences_pending_cross_sell(pending, text, expected):
    from src.ports.outbound.tool_executor_port import ToolResult
    from tests.fakes.fake_tool_executor import FakeToolExecutor
    from tests.unit.test_interpret_and_respond_use_case import build, reply, run
    tools = FakeToolExecutor({"silenciar_complementos": ToolResult("OK", {"texto": "Silenciado"})})
    engine, conversation, _, _, _, _ = build([reply("Respuesta normal")], tools=tools)
    conversation.context = {"crossSellPending": pending}
    await run(engine, conversation, text)
    assert [value[0] for value in tools.executed] == ([expected] if expected else [])


@pytest.mark.asyncio
async def test_below_budget_reports_only_observed_minimum():
    uc, ctx = service()
    result = await uc.recommend(RecommendationArguments(actividad="running", presupuestoMax=30), ctx)
    assert "S/ 80.00" in result["texto"]
    assert result["bloques"][0]["datos"]["acciones"][0]["payload"]["presupuestoMax"] == 80


@pytest.mark.asyncio
async def test_missing_prices_are_null_and_cannot_be_recommended():
    uc, ctx = service()
    uc._catalog.get_prices = AsyncMock(return_value=[])
    search = await uc.search(SearchArguments(q="running"), ctx)
    assert all(row["price"] is None and row["price_text"] == "Precio no disponible"
               for row in search["bloques"][0]["datos"]["productos"])
    assert (await uc.recommend(RecommendationArguments(actividad="running"), ctx))["bloques"] == []
