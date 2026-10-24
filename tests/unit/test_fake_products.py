"""Free-text search of the in-process Productos fake (ADR-0022).

The LLM often sends the whole request as `q` ("zapatillas nike",
"productos nike") instead of the `categoria` and `marca` filters, so the
fake matches each word against name, description, brand and category.
"""
import pytest

from src.adapters.outbound.fakes.fake_products import FakeCatalogPort


async def total_for(q: str | None, **filters) -> int:
    page = await FakeCatalogPort().search_products(q=q, **filters)
    return page["total"]


@pytest.mark.asyncio
async def test_category_word_in_q_matches_products_of_that_category():
    assert await total_for("zapatillas") == await total_for(None, category_id="cat-zapatillas")


@pytest.mark.asyncio
async def test_category_and_brand_words_in_q_combine_as_and():
    expected = await total_for(None, category_id="cat-zapatillas", brand_id="brand-nike")

    assert expected > 0
    assert await total_for("zapatillas nike") == expected


@pytest.mark.asyncio
async def test_filler_words_are_ignored():
    assert await total_for("aver productos nike") == await total_for("nike")


@pytest.mark.asyncio
async def test_matching_ignores_case_and_accents():
    assert await total_for("ZAPATILLAS Nike") == await total_for("zapatillas nike")
    assert await total_for("amortiguación") == await total_for("amortiguacion")
    assert await total_for("amortiguacion") > 0


@pytest.mark.asyncio
async def test_singular_word_matches_plural_text():
    assert await total_for("zapatilla") == await total_for("zapatillas")


@pytest.mark.asyncio
async def test_word_present_in_no_product_returns_nothing():
    assert await total_for("zapatillas zzz") == 0


@pytest.mark.asyncio
async def test_name_search_still_works():
    assert await total_for("Revolution") >= 1


@pytest.mark.asyncio
async def test_q_made_only_of_filler_words_does_not_filter():
    assert await total_for("productos") == await total_for(None)
