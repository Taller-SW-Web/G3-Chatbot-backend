"""Similar-product fallback (SPEC-10): same category, ±20% price, with stock. Pure."""
from decimal import Decimal


class SimilarProducts:
    def find(
        self,
        category_id: str | None,
        price: Decimal | float | int | None,
        candidates: list[dict],
        *,
        available_skus: set[str] | None = None,
        exclude: set[str] | None = None,
        limit: int = 3,
    ) -> list[dict]:
        if category_id is None or price is None:
            return []
        excluded = exclude or set()
        stocked = available_skus
        reference = Decimal(str(price))
        low, high = reference * Decimal("0.8"), reference * Decimal("1.2")
        scored: list[dict] = []
        for candidate in candidates:
            product_id = candidate.get("product_id")
            if product_id in excluded:
                continue
            if candidate.get("category_id") != category_id:
                continue
            amount = candidate.get("precio_oferta") or candidate.get("precio_regular")
            if amount is None:
                continue
            if not low <= Decimal(str(amount)) <= high:
                continue
            if stocked is not None and product_id not in stocked:
                continue
            scored.append(candidate)
            if len(scored) >= limit:
                break
        return scored
