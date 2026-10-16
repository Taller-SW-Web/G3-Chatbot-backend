"""Natural-language variant resolution (SPEC-09).

Maps spoken attributes ("42", "negra") to a product variant SKU, or
reports what is missing / which close combinations exist. Pure.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class VariantMatch:
    sku: str | None = None
    missing: tuple[str, ...] = ()
    alternatives: tuple[dict] = ()


def _fold(text: str) -> str:
    return text.strip().lower()


class VariantResolver:
    def resolve(
        self,
        detail: dict,
        *,
        size: str | None = None,
        color: str | None = None,
    ) -> VariantMatch:
        variants = [v for v in detail.get("variants", []) if isinstance(v, dict)]
        if not variants:
            sku = detail.get("sku_base")
            return VariantMatch(sku=sku) if sku else VariantMatch()
        wanted_size = _fold(size) if size else None
        wanted_color = _fold(color) if color else None
        scored: list[tuple[int, dict]] = []
        for variant in variants:
            score = 0
            if wanted_size is not None and _fold(str(variant.get("talla", ""))) == wanted_size:
                score += 1
            if wanted_color is not None and _fold(str(variant.get("color", ""))) == wanted_color:
                score += 1
            scored.append((score, variant))
        asked = (1 if wanted_size else 0) + (1 if wanted_color else 0)
        exact = [variant for score, variant in scored if asked and score == asked]
        if len(exact) == 1 and exact[0].get("sku"):
            return VariantMatch(sku=exact[0]["sku"])
        missing: list[str] = []
        if wanted_size is None:
            missing.append("talla")
        if wanted_color is None:
            missing.append("color")
        if not missing and not exact:
            return VariantMatch(alternatives=tuple(v for _, v in sorted(scored, key=lambda pair: pair[0], reverse=True)[:3]))
        if missing:
            pool = [v for _, v in scored] if not exact else exact
            return VariantMatch(
                missing=tuple(missing), alternatives=tuple(pool[:3])
            )
        return VariantMatch(alternatives=tuple(v for _, v in sorted(scored, key=lambda pair: pair[0], reverse=True)[:3]))
