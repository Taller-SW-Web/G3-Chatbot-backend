"""In-process fake Productos adapters (SPEC-06, SPEC-09, SPEC-10, SPEC-11, ADR-0022).

Selected with `MODULES_MODE=fake` for deployed environments without real
modules. They implement the same outbound ports as the HTTP clients and
share the seed data in `seeds/` (also used by the respx test mocks), so
demo data stays deterministic. No network, no database.
"""
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from src.ports.outbound.module_errors import ModuleRequestError

SEEDS = Path(__file__).resolve().parent / "seeds"

# Words the LLM adds around the actual search terms ("aver productos nike").
FILLER_WORDS = frozenset({
    "a", "al", "aver", "busco", "buscar", "con", "de", "del", "el", "en", "hay",
    "la", "las", "lo", "los", "me", "mostrar", "muestra", "muestrame", "para",
    "por", "producto", "productos", "que", "quiero", "tiene", "tienen", "tienes",
    "un", "una", "unas", "unos", "ver", "y",
})


def load_seed(name: str):
    with open(SEEDS / name, encoding="utf-8") as handle:
        return json.load(handle)


def _fold_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _search_words(q: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", _fold_accents(q))
    return [word for word in words if word not in FILLER_WORDS]


class FakeCatalogPort:
    """Catalog fake over the seed catalog (100 products)."""

    def __init__(
        self,
        products: list[dict] | None = None,
        details: dict[str, dict] | None = None,
        prices: dict[str, dict] | None = None,
    ):
        self._products = products if products is not None else load_seed(
            "products_catalog.json"
        )
        self._details = details if details is not None else {
            "prod-001": load_seed("product_detail.json")
        }
        if prices is not None:
            self._prices = prices
        else:
            self._prices = {
                "NIK-REV7-42-NEG": {
                    "sku": "NIK-REV7-42-NEG", "precio_regular": 329.9,
                    "precio_oferta": 279.9, "currency": "PEN",
                }
            }
        self._sku_index: dict[str, dict] | None = None
        self._category_names = {
            category["category_id"]: category["name"]
            for category in load_seed("categories.json")
        }

    def _searchable_text(self, item: dict) -> str:
        """Name, description, brand and category name, folded for word matching.

        A word matches as a substring, so "zapatilla" also finds "zapatillas".
        """
        parts = (
            item.get("name"), item.get("description"), item.get("brand"),
            self._category_names.get(item.get("category_id")),
        )
        return _fold_accents(" ".join(part for part in parts if part))

    def _sku_product(self, sku: str) -> dict | None:
        """Product owning a SKU: exact variant match, else sku_base prefix."""
        if self._sku_index is None:
            index: dict[str, dict] = {}
            for item in self._products:
                base = str(item.get("sku_base") or "")
                if base:
                    index.setdefault(base, item)
            for detail in self._details.values():
                for variant in detail.get("variants", []):
                    if variant.get("sku"):
                        index[str(variant["sku"])] = {
                            "product_id": detail.get("product_id"),
                            "name": detail.get("name"),
                            "precio_regular": variant.get("precio_regular"),
                            "precio_oferta": variant.get("precio_oferta"),
                        }
            self._sku_index = index
        if sku in self._sku_index:
            return self._sku_index[sku]
        for base, item in self._sku_index.items():
            if base and sku.startswith(base):
                return item
        return None

    async def search_products(self, *, q=None, category_id=None, brand_id=None,
                              channel="CHATBOT", page=1, size=10, state="ACTIVO",
                              min_price=None, max_price=None, correlation_id=None):
        words = _search_words(q) if q else []
        items = [
            item for item in self._products
            if all(word in self._searchable_text(item) for word in words)
            and (category_id is None or item.get("category_id") == category_id)
            and (brand_id is None or item.get("brand_id") == brand_id)
        ]
        start = (page - 1) * size
        return {
            "items": items[start:start + size], "pagina": page,
            "tamanio": size, "total": len(items),
        }

    async def get_product_detail(self, product_id, *, correlation_id=None):
        if product_id in self._details:
            return self._details[product_id]
        for item in self._products:
            if item.get("product_id") == product_id:
                base = item.get("sku_base") or product_id
                return {
                    "product_id": product_id, "name": item.get("name"),
                    "description": item.get("description"),
                    "brand_id": item.get("brand_id"),
                    "category_id": item.get("category_id"),
                    "images": item.get("images", []),
                    "tiene_variantes": False,
                    "variants": [{"sku": f"{base}-STD"}],
                }
        raise ModuleRequestError("productos", 404, "RECURSO_NO_ENCONTRADO")

    async def list_categories(self, *, correlation_id=None):
        return load_seed("categories.json")

    async def list_brands(self, *, correlation_id=None):
        return load_seed("brands.json")

    async def get_price(self, sku, *, correlation_id=None):
        if sku in self._prices:
            return self._prices[sku]
        item = self._sku_product(sku)
        if item is None:
            raise ModuleRequestError("productos", 404, "RECURSO_NO_ENCONTRADO")
        return {
            "sku": sku,
            "precio_regular": item.get("precio_regular", 0),
            "precio_oferta": item.get("precio_oferta"),
            "currency": "PEN",
        }

    async def get_prices(self, skus, *, channel="CHATBOT", correlation_id=None):
        out = []
        for sku in skus:
            try:
                out.append(await self.get_price(sku, correlation_id=correlation_id))
            except ModuleRequestError:
                continue
        return out

    async def evaluate_promotions(self, lines, *, channel_id="CHATBOT",
                                  coupon_code=None, customer_ref=None,
                                  correlation_id=None):
        total = 0
        for line in lines:
            try:
                row = await self.get_price(line["sku"])
            except ModuleRequestError:
                continue
            amount = row.get("precio_oferta") or row.get("precio_regular", 0)
            total += amount * line.get("quantity", 1)
        discount = round(total * 0.1, 2) if coupon_code == "SPORT10" else 0
        return {
            "discount_amount": discount,
            "promotion_id": "promo-10" if discount else None,
            "coupon_applied": coupon_code in (None, "SPORT10"),
        }

    async def list_promotions(self, *, correlation_id=None):
        return [{"promotion_id": "promo-10", "name": "Cyber 10%"}]


class FakeInventoryPort:
    """Inventory fake: configurable statuses, DISPONIBLE by default."""

    def __init__(self, statuses: dict[str, str] | None = None,
                 default: str = "DISPONIBLE"):
        self._statuses = dict(statuses or {})
        self._default = default

    async def get_availability(self, skus, *, channel="CHATBOT",
                               correlation_id=None):
        return [
            {"sku": sku, "status": self._statuses.get(sku, self._default)}
            for sku in skus
        ]

    async def verify_quantities(self, lines, *, channel_id="CHATBOT",
                                correlation_id=None):
        verdicts = []
        for line in lines:
            status = self._statuses.get(line["sku"], self._default)
            verdict = (
                "SUFICIENTE" if status in ("DISPONIBLE", "STOCK_BAJO")
                else "INSUFICIENTE" if status == "AGOTADO" else "INDETERMINADO"
            )
            verdicts.append({
                "sku": line["sku"], "quantity": line["quantity"],
                "veredicto": verdict,
            })
        return verdicts

    async def get_sku_availability(self, sku, *, correlation_id=None):
        rows = await self.get_availability([sku], correlation_id=correlation_id)
        return rows[0] if rows else None


def availability_seeds() -> Any:
    return load_seed("availability.json")


def availability_verify_seeds() -> Any:
    return load_seed("availability_verify.json")
