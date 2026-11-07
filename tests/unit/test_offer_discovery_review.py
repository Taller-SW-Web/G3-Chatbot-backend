"""Bounded chat discovery and the separate upstream REST page contract."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from src.application.tools.catalog_tools import PromotionArguments, RecommendationArguments, SearchArguments
from src.application.use_cases import manage_catalog_use_case as module
from src.application.use_cases.manage_catalog_use_case import CatalogError
from src.ports.outbound.module_errors import ModuleRequestError
from tests.unit.test_astra_regressions import cards, g6_detail, g6_service
from tests.unit.test_discovery import PRICE


def paged(*, offers=(), total=30, duplicate=False):
    details = [g6_detail(str(i), "brand") for i in range(total)]
    uc, ctx = g6_service(details)
    uc._catalog.list_promotions = AsyncMock(return_value=[])

    async def search(**kw):
        start = (kw.get("page", 1) - 1) * kw.get("size", 10)
        rows = [{"product_id": str(i)} for i in range(start, min(start + kw.get("size", 10), total))]
        if duplicate and start == 10:
            rows[0] = {"product_id": "0"}
        return {"items": rows, "total": total}

    async def prices(skus):
        return [{**PRICE, "sku": sku, "precio_oferta": 50 if int(sku.split("-")[0]) in offers else None}
                for sku in skus]

    uc._catalog.search_products = AsyncMock(side_effect=search)
    uc._catalog.get_prices = AsyncMock(side_effect=prices)
    return uc, ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("offer", [10, 25])
async def test_offers_beyond_first_page_exhaust_catalog_and_count_exactly(offer):
    uc, ctx = paged(offers=[offer])
    result = await uc.offers(PromotionArguments(), ctx)
    assert [row["product_id"] for row in cards(result)] == [str(offer)]
    assert result["total_scope"] == "catalog" and result["complete"] is True
    assert result["pages_scanned"] == 3
    assert "Encontré 1 productos en oferta en el catálogo" in result["texto"]


@pytest.mark.asyncio
async def test_duplicate_products_are_quoted_and_emitted_once():
    uc, ctx = paged(offers=[0, 12], duplicate=True)
    result = await uc.offers(PromotionArguments(), ctx)
    assert [row["product_id"] for row in cards(result)] == ["0", "12"]
    assert sum(call.args == ("0",) for call in uc._catalog.get_product_detail.await_args_list) == 1


@pytest.mark.asyncio
async def test_page_limit_is_explicit_even_when_empty():
    uc, ctx = paged(offers=[55], total=60)
    result = await uc.offers(PromotionArguments(), ctx)
    assert cards(result) == [] and result["total"] == 0
    assert result["total_scope"] == "scanned_pages" and result["complete"] is False
    assert result["pages_scanned"] == 5
    assert "parcial" in result["texto"] and "puede haber más ofertas" in result["texto"]
    assert uc._catalog.search_products.await_count == 5


@pytest.mark.asyncio
async def test_enough_offers_stop_scan_without_claiming_catalog_total():
    uc, ctx = paged(offers=range(30))
    result = await uc.offers(PromotionArguments(), ctx)
    assert len(cards(result)) == 10 and result["pages_scanned"] == 1
    assert not result["complete"] and result["total_scope"] == "scanned_pages"
    assert "páginas consultadas" in result["texto"]


@pytest.mark.asyncio
async def test_last_page_counts_all_matches_even_beyond_carousel_size():
    uc, ctx = paged(offers=range(1, 20), total=20)
    result = await uc.offers(PromotionArguments(), ctx)
    assert len(cards(result)) == 10 and result["total"] == 19
    assert result["complete"] and result["total_scope"] == "catalog"
    assert "Encontré 19" in result["texto"] and "Te muestro 10" in result["texto"]


@pytest.mark.asyncio
async def test_upstream_timeout_is_error_rather_than_empty_success():
    uc, ctx = paged()
    uc._catalog.search_products.side_effect = TimeoutError("supplier failed")
    with pytest.raises(CatalogError, match="TimeoutError"):
        await uc.offers(PromotionArguments(), ctx)


@pytest.mark.asyncio
async def test_unknown_total_requires_empty_page_to_confirm_exhaustion():
    uc, ctx = paged(total=3)
    original = uc._catalog.search_products.side_effect

    async def unknown(**kw):
        page = await original(**kw)
        page.pop("total")
        return page

    uc._catalog.search_products.side_effect = unknown
    result = await uc.offers(PromotionArguments(), ctx)
    assert result["complete"] and result["pages_scanned"] == 2
    assert result["total"] == 0 and result["total_scope"] == "catalog"


@pytest.mark.asyncio
async def test_deadline_preserves_only_completed_pages(monkeypatch):
    uc, ctx = paged(offers=[0])
    original = uc._catalog.search_products.side_effect

    async def slow(**kw):
        if kw["page"] == 2:
            await asyncio.sleep(1)
        return await original(**kw)

    uc._catalog.search_products.side_effect = slow
    monkeypatch.setattr(module, "OFFER_DISCOVERY_SECONDS", 0.1)
    result = await uc.offers(PromotionArguments(), ctx)
    assert [row["product_id"] for row in cards(result)] == ["0"]
    assert not result["complete"] and result["pages_scanned"] == 1
    assert "parcial" in result["texto"]


@pytest.mark.asyncio
async def test_later_supplier_error_preserves_rate_and_retry_after():
    uc, ctx = paged(offers=[0])
    rejected = ModuleRequestError("productos", 429, "RATE_LIMIT", "slow down")
    rejected.retry_after = "9"
    uc._catalog.search_products.side_effect = [
        {"items": [{"product_id": "0"}], "total": 30},
        rejected,
    ]
    with pytest.raises(CatalogError) as error:
        await uc.offers(PromotionArguments(), ctx)
    assert error.value.status == 429 and error.value.retry_after == "9"


@pytest.mark.asyncio
async def test_rest_offer_filter_retains_requested_page_size_and_filters():
    uc, ctx = paged(offers=[12, 25])
    result = await uc.search(SearchArguments(soloOfertas=True, pagina=3, tamanio=5,
                                            categoriaId="cat", marcaId="brand", precioMin=10), ctx)
    assert cards(result)[0]["product_id"] == "12"
    kwargs = uc._catalog.search_products.await_args.kwargs
    assert kwargs["page"] == 3 and kwargs["size"] == 5
    assert kwargs["category_id"] == "cat" and kwargs["brand_id"] == "brand" and kwargs["min_price"] == 10
    assert uc._catalog.search_products.await_count == 1
    assert result["total_scope"] == "page" and "en esta página" in result["texto"]


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,expected", [("m", ["p"]), ("M", ["p"]), ("M/L", []), ("MM", [])])
async def test_recommendations_casefold_sizes_without_collapsing_distinct_sizes(requested, expected):
    uc, ctx = g6_service([g6_detail("p", "brand", ("M", "L"))])
    result = await uc.recommend(RecommendationArguments(actividad="running", talla=requested), ctx)
    assert [row["product_id"] for row in cards(result)] == expected
