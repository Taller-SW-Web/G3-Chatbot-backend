"""The search tool text must state exactly what the carousel shows."""
import pytest

from src.application.tools.catalog_tools import SearchArguments
from tests.unit.test_astra_regressions import cards
from tests.unit.test_offer_discovery_review import paged


@pytest.mark.asyncio
async def test_catalog_total_above_carousel_size_states_how_many_are_shown():
    uc, ctx = paged(total=30)
    result = await uc.search(SearchArguments(), ctx)
    assert len(cards(result)) == 10 and result["total"] == 30
    assert "Encontré 30 productos" in result["texto"]
    assert "se muestran 10" in result["texto"]


@pytest.mark.asyncio
async def test_all_results_shown_does_not_mention_a_subset():
    uc, ctx = paged(total=3)
    result = await uc.search(SearchArguments(), ctx)
    assert len(cards(result)) == 3
    assert "Encontré 3 productos" in result["texto"]
    assert "se muestran en pantalla" in result["texto"]


@pytest.mark.asyncio
async def test_local_filter_that_empties_the_page_returns_no_carousel():
    uc, ctx = paged(total=5)
    result = await uc.search(SearchArguments(soloOfertas=True), ctx)
    assert result["bloques"] == [] and result["total"] == 0
    assert result["total_scope"] == "page"
    assert "No encontré productos" in result["texto"]
    assert "pantalla" not in result["texto"]
