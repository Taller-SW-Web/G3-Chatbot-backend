"""Translate G6 commercial attributes into the internal product vocabulary."""


def _attribute_value(value):
    """G6 value references carry a display name; legacy payloads use strings."""
    if isinstance(value, dict):
        value = value.get("nombre")
    return value.strip() or None if isinstance(value, str) else None


def normalize_detail(data):
    variants = []
    for variant in data.get("variants") or []:
        attributes = {
            row["nombre"].strip().casefold(): _attribute_value(row.get("valor"))
            for row in variant.get("identifying_attributes") or []
            if isinstance(row, dict) and isinstance(row.get("nombre"), str)
        }
        variants.append(
            {
                **variant,
                "talla": attributes.get("talla", _attribute_value(variant.get("talla"))),
                "color": attributes.get("color", _attribute_value(variant.get("color"))),
            }
        )
    brand = data.get("marca") or {}
    return {
        **data,
        "variants": variants,
        "brand_id": data.get("marca_id") or brand.get("marca_id") or data.get("brand_id"),
        "brand_name": brand.get("name") or data.get("brand_name"),
        "category_id": data.get("categoria_id") or data.get("category_id"),
    }
