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


@pytest.mark.asyncio
async def test_price_and_currency_phrasing_in_q_is_not_a_required_word():
    assert await total_for("zapatillas de menos de 350 soles") == await total_for("zapatillas")


@pytest.mark.asyncio
async def test_colloquial_running_terms_match_running_descriptions():
    assert await total_for("zapatillas para correr") > 0
    assert await total_for("zapatillas para correr") == await total_for("zapatillas running")


@pytest.mark.asyncio
async def test_max_price_applies_to_effective_offer_price():
    # prod-001: regular 329.90, offer 279.90.
    page = await FakeCatalogPort().search_products(q="Revolution", max_price=300)
    assert [row["product_id"] for row in page["items"]] == ["prod-001"]
    page = await FakeCatalogPort().search_products(q="Revolution", max_price=250)
    assert page["items"] == []


@pytest.mark.asyncio
async def test_min_price_excludes_cheaper_effective_price():
    assert (await FakeCatalogPort().search_products(q="Revolution", min_price=300))["items"] == []
    assert (await FakeCatalogPort().search_products(q="Revolution", min_price=279.9))["total"] == 1


@pytest.mark.asyncio
async def test_fake_evaluation_discounts_the_valid_offer_per_unit():
    # NIK-REV7: regular 329.90, offer 279.90 -> 50.00 per unit.
    result = await FakeCatalogPort().evaluate_promotions([{"sku": "NIK-REV7-42-NEG", "quantity": 2}])

    assert result["original_amount"] == 659.8
    assert result["discount_amount"] == 50.0 * 2
    assert result["result_amount"] == 559.8
    assert result["promotion_id"] is None


@pytest.mark.asyncio
async def test_fake_evaluation_has_no_discount_without_offer_and_keeps_the_coupon():
    # ADI-PRED: regular 349.00, no offer.
    plain = await FakeCatalogPort().evaluate_promotions([{"sku": "ADI-PRED-42-NEG", "quantity": 1}])
    assert plain["discount_amount"] == 0 and plain["result_amount"] == 349.0

    coupon = await FakeCatalogPort().evaluate_promotions(
        [{"sku": "ADI-PRED-42-NEG", "quantity": 1}], coupon_code="SPORT10")
    assert coupon["discount_amount"] == 34.9 and coupon["promotion_id"] == "promo-10"


def test_seed_images_are_real_unsplash_urls_with_a_two_image_gallery():
    from src.adapters.outbound.fakes.fake_products import load_seed

    catalog = load_seed("products_catalog.json")
    for row in catalog:
        assert len(row["images"]) == 2 and row["images"][0] != row["images"][1]
        assert all(url.startswith("https://images.unsplash.com/photo-") for url in row["images"])
    detail = load_seed("product_detail.json")
    first = next(row for row in catalog if row["product_id"] == detail["product_id"])
    assert detail["images"] == first["images"]
