"""Product detail and variant resolution (SPEC-09 Req. 3-4). No network, no database."""
from src.domain.services.variant_resolver import VariantResolver

DETAIL = {
    "product_id": "prod-001",
    "name": "Nike Revolution 7",
    "variants": [
        {"sku": "NIK-REV7-42-NEG", "talla": "42", "color": "negro"},
        {"sku": "NIK-REV7-42-BLA", "talla": "42", "color": "blanco"},
        {"sku": "NIK-REV7-43-NEG", "talla": "43", "color": "negro"},
    ],
}


def test_full_attributes_resolve_sku():
    match = VariantResolver().resolve(DETAIL, size="42", color="negra")
    assert match.sku is None  # "negra" is not "negro": no exact match
    match = VariantResolver().resolve(DETAIL, size="42", color="negro")
    assert match.sku == "NIK-REV7-42-NEG"


def test_missing_attribute_reports_what_is_missing():
    match = VariantResolver().resolve(DETAIL, size="42")
    assert match.sku is None
    assert "color" in match.missing
    assert match.alternatives


def test_unknown_combination_suggests_alternatives():
    match = VariantResolver().resolve(DETAIL, size="44", color="rojo")
    assert match.sku is None
    assert len(match.alternatives) == 3


def test_product_without_variants_uses_base_sku():
    match = VariantResolver().resolve({"sku_base": "NIK-POLO"})
    assert match.sku == "NIK-POLO"
