"""Catalog use case: search, detail and availability (SPEC-06, SPEC-09, SPEC-10).

Orchestrates the Productos ports, resolves references against the
conversation working memory (`ultimoCarrusel`, `filtrosVigentes`) and
returns plain data; handlers wrap it into `ToolResult` blocks. Never
imports FastAPI, httpx or SQLAlchemy.
"""
import asyncio
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from src.application.catalog.product_lookup import (
    MEMORY_CAROUSEL_KEY,
    MEMORY_FILTERS_KEY,
    resolve_product_id,
)
from src.application.tools.tool_context import (
    caller_from_context,
    conversation_id_from_context,
)
from src.domain.services.product_normalizer import ProductNormalizer
from src.domain.services.similar_products import SimilarProducts
from src.domain.services.stock_validator import StockValidator
from src.domain.services.variant_resolver import VariantResolver
from src.ports.outbound.module_errors import (
    ModuleRequestError,
    ModuleUnavailableError,
)

LIST_TTL_SECONDS = 600.0


class OptionCache:
    """In-process categories/brands cache (SPEC-06).

    Injectable so tests and `MODULES_MODE` runs never share stale lists: each
    use case gets its own cache unless the wiring passes a shared instance.
    """

    def __init__(self, ttl_seconds: float = LIST_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[float, list]] = {}

    def get(self, key: str) -> list | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        cached_at, values = entry
        if time.monotonic() - cached_at >= self._ttl_seconds:
            return None
        return values

    def put(self, key: str, values: list) -> None:
        self._entries[key] = (time.monotonic(), values)

    def clear(self) -> None:
        self._entries.clear()


class CatalogError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(detail or code)


class ManageCatalogUseCase:
    def __init__(
        self,
        catalog,
        inventory,
        conversations,
        uow,
        normalizer: ProductNormalizer | None = None,
        option_cache: OptionCache | None = None,
    ):
        self._catalog = catalog
        self._inventory = inventory
        self._conversations = conversations
        self._uow = uow
        self._normalizer = normalizer
        self._option_cache = option_cache if option_cache is not None else OptionCache()
        self._variants = VariantResolver()
        self._stock = StockValidator()
        self._similar = SimilarProducts()

    @property
    def inventory(self):
        """Inventory port for punctual availability reads (routers)."""
        return self._inventory

    # -- public orchestration -------------------------------------------------

    async def search(self, args, context) -> dict[str, Any]:
        conversation_id = await self._owned_conversation(context)
        memory = await self._read_memory(conversation_id)
        categories = await self._cached_list("categories", self._catalog.list_categories)
        brands = await self._cached_list("brands", self._catalog.list_brands)
        normalizer = self._normalizer or ProductNormalizer(
            categories=categories, brands=brands
        )
        filters = normalizer.normalize_filters(args.q, args.categoria, args.marca)
        if filters.unresolved_category or filters.unresolved_brand:
            return {
                "code": "OK",
                "texto": self._clarifying_question(filters),
                "bloques": [],
            }
        page = await self._catalog.search_products(
            q=filters.q,
            category_id=filters.category_id,
            brand_id=filters.brand_id,
            channel=args.canal,
            page=args.pagina,
            size=args.tamanio,
            state=args.estado,
            min_price=args.precioMin,
            max_price=args.precioMax,
        )
        items = page.get("items", [])
        if not items:
            return {
                "code": "OK",
                "texto": (
                    "No encontré productos con esos filtros. "
                    "Prueba quitando un filtro o buscando por categoría."
                ),
                "bloques": [],
            }
        summaries = await self._summarize(items[:10])
        memory[MEMORY_CAROUSEL_KEY] = [s["product_id"] for s in summaries]
        memory[MEMORY_FILTERS_KEY] = {
            "q": filters.q,
            "category_id": filters.category_id,
            "brand_id": filters.brand_id,
        }
        await self._save_memory(conversation_id, memory)
        total = page.get("total", len(summaries))
        return {
            "code": "OK",
            "texto": f"Encontré {total} productos. Te muestro los primeros:",
            "bloques": [{
                "id": uuid.uuid4().hex,
                "tipo": "CARRUSEL_PRODUCTOS",
                "datos": {"productos": summaries},
            }],
            "total": total,
        }

    async def detail(self, args, context) -> dict[str, Any]:
        conversation_id = await self._owned_conversation(context)
        memory = await self._read_memory(conversation_id)
        product_id = resolve_product_id(
            memory, producto_ref=args.productoRef, product_id=args.product_id
        )
        if product_id is None:
            return {
                "code": "OK",
                "texto": "¿De qué producto quieres el detalle? Dime el número del carrusel.",
                "bloques": [],
            }
        try:
            data = await self._catalog.get_product_detail(product_id)
        except Exception as exc:
            raise self._map_module_error(exc, inactive=True) from exc
        summary = (await self._summarize([data], prefetched={product_id: data}))[0]
        return {
            "code": "OK",
            "texto": f"{summary['name']}: {summary['price_text']}",
            "bloques": [{
                "id": uuid.uuid4().hex,
                "tipo": "DETALLE_PRODUCTO",
                "datos": {"producto": summary},
            }],
        }

    async def availability(self, args, context) -> dict[str, Any]:
        conversation_id = await self._owned_conversation(context)
        memory = await self._read_memory(conversation_id)
        product_id = resolve_product_id(
            memory, producto_ref=args.productoRef, product_id=args.product_id
        )
        if product_id is None:
            return {
                "code": "OK",
                "texto": "¿La disponibilidad de qué producto consulto?",
                "bloques": [],
            }
        try:
            data = await self._catalog.get_product_detail(product_id)
        except Exception as exc:
            raise self._map_module_error(exc, inactive=True) from exc
        match = self._variants.resolve(
            data, size=getattr(args, "talla", None),
            color=getattr(args, "color", None),
        )
        if match.sku is None:
            if match.missing:
                return {
                    "code": "OK",
                    "texto": f"¿En qué {' y '.join(match.missing)} lo buscas?",
                    "bloques": [{
                        "id": uuid.uuid4().hex,
                        "tipo": "SELECTOR_VARIANTE",
                        "datos": {
                            "product_id": product_id,
                            "faltantes": list(match.missing),
                        },
                    }],
                }
            return {
                "code": "OK",
                "texto": "Esa combinación no existe. Te muestro alternativas cercanas.",
                "bloques": [],
            }
        rows = await self._inventory.get_availability([match.sku])
        check = self._stock.check_row(rows[0] if rows else None)
        if check.ok and not check.warning:
            return {"code": "OK", "texto": "Sí, hay stock disponible.", "bloques": []}
        if check.ok:
            return {
                "code": "OK",
                "texto": "Hay pocas unidades. Si te interesa, agrégalo pronto.",
                "bloques": [],
            }
        similar = await self._find_similar(data, await self._reference_price(data))
        return {
            "code": "OK",
            "texto": "Ese SKU está agotado. Te muestro alternativas similares.",
            "bloques": [{
                "id": uuid.uuid4().hex,
                "tipo": "CARRUSEL_PRODUCTOS",
                "datos": {"productos": similar},
            }] if similar else [],
        }

    # -- internals ------------------------------------------------------------

    async def _owned_conversation(self, context):
        """Id of the owned conversation, or None for memory-less direct reads.

        REST calls without a chat context skip the carousel memory (ordinals
        stay unavailable) but can still search by explicit ids.
        """
        conversation_id = conversation_id_from_context(context)
        if conversation_id is None:
            return None
        caller = caller_from_context(context)
        if caller is None:
            raise CatalogError("RECURSO_NO_ENCONTRADO", "missing caller")
        if await self._conversations.find_owned(conversation_id, caller) is None:
            raise CatalogError("RECURSO_NO_ENCONTRADO", "conversation not found")
        return conversation_id

    async def _read_memory(self, conversation_id) -> dict:
        if conversation_id is None:
            return {}
        return await self._conversations.get_context(conversation_id)

    async def _save_memory(self, conversation_id, memory: dict) -> None:
        if conversation_id is None:
            return
        try:
            await self._conversations.save_context(conversation_id, memory)
            await self._uow.commit()
        except BaseException:
            await self._uow.rollback()
            raise

    async def _cached_list(self, key: str, loader) -> list:
        cached = self._option_cache.get(key)
        if cached is not None:
            return cached
        values = await loader()
        self._option_cache.put(key, values)
        return values

    async def _summarize(
        self, items: list[dict], prefetched: dict[str, dict] | None = None
    ) -> list[dict]:
        """Summaries for up to 10 carousel items, batching the fan-out.

        Details are fetched concurrently and prices/availability are asked in
        two bulk calls for every SKU, instead of two calls per product.
        """
        prefetched = prefetched or {}
        resolved: list[tuple[int, dict]] = []
        missing: list[tuple[int, str]] = []
        for position, item in enumerate(items, start=1):
            product_id = str(item.get("product_id", ""))
            data = prefetched.get(product_id)
            if data is None:
                missing.append((position, product_id))
            else:
                resolved.append((position, data))
        if missing:
            fetched = await asyncio.gather(
                *(self._catalog.get_product_detail(pid) for _, pid in missing),
                return_exceptions=True,
            )
            for (position, _), data in zip(missing, fetched):
                if isinstance(data, BaseException):
                    continue
                resolved.append((position, data))
        resolved.sort(key=lambda entry: entry[0])
        skus = [
            str(variant.get("sku"))
            for _, data in resolved
            for variant in (data.get("variants") or [])
            if variant.get("sku")
        ]
        prices = await self._prices_by_sku(skus) if skus else {}
        statuses = await self._status_by_sku(skus) if skus else {}
        return [
            self._summary(data, position, prices, statuses)
            for position, data in resolved
        ]

    async def _prices_by_sku(self, skus: list[str]) -> dict[str, dict]:
        try:
            rows = await self._catalog.get_prices(skus)
        except Exception:
            return {}
        return {str(row.get("sku")): row for row in rows if row.get("sku")}

    async def _status_by_sku(self, skus: list[str]) -> dict[str, str]:
        try:
            rows = await self._inventory.get_availability(skus)
        except Exception:
            return {}
        return {str(row.get("sku")): str(row.get("status", "")) for row in rows}

    @staticmethod
    def _price_from_variants(variants: list, prices: dict) -> Decimal:
        """Lowest offer/regular price across variants (0 when unknown).

        Prices may be absent or `null` (Productos 0.5.0): those variants are
        skipped instead of breaking the conversion.
        """
        amounts: list[Decimal] = []
        for variant in variants:
            row = prices.get(str(variant.get("sku")))
            if not row:
                continue
            amount = row.get("precio_oferta")
            if amount is None:
                amount = row.get("precio_regular")
            if amount is None:
                continue
            try:
                amounts.append(Decimal(str(amount)))
            except (InvalidOperation, ValueError):
                continue
        return min(amounts) if amounts else Decimal("0")

    def _summary(
        self, data: dict, position: int, prices: dict, statuses: dict
    ) -> dict[str, Any]:
        variants = data.get("variants") or []
        price = self._price_from_variants(variants, prices)
        name = str(data.get("name", ""))
        description = data.get("description") or f"{name} {data.get('brand_id', '')}".strip()
        images = data.get("images") or []
        return {
            "ref": position,
            "product_id": str(data.get("product_id", "")),
            "name": name,
            "brand": data.get("brand_id"),
            "image": images[0] if images else None,
            "price": price,
            "price_text": f"S/ {price:.2f}",
            "short_description": str(description)[:90],
            "has_variants": bool(data.get("tiene_variantes") and variants),
            "single_sku": str(variants[0].get("sku")) if len(variants) == 1 else None,
            "availability": self._badge(statuses, variants),
            "variants": [
                {"sku": str(v.get("sku")), "talla": v.get("talla"), "color": v.get("color")}
                for v in variants if v.get("sku")
            ],
        }

    @staticmethod
    def _badge(statuses: dict, variants: list) -> str:
        if not variants:
            return "DISPONIBLE"
        values = {statuses.get(str(v.get("sku")), "") for v in variants if v.get("sku")}
        if "DISPONIBLE" in values:
            return "DISPONIBLE"
        if "STOCK_BAJO" in values:
            return "POCAS_UNIDADES"
        if "AGOTADO" in values and len(values) == 1:
            return "AGOTADO"
        if "AGOTADO" in values:
            return "POCAS_UNIDADES"
        return "INDETERMINADO"

    async def _reference_price(self, data: dict) -> Decimal:
        """Price of the current product, from its SKU prices (detail carries none)."""
        variants = data.get("variants") or []
        skus = [str(v.get("sku")) for v in variants if v.get("sku")]
        prices = await self._prices_by_sku(skus) if skus else {}
        return self._price_from_variants(variants, prices)

    async def _find_similar(self, data: dict, reference: Decimal) -> list[dict]:
        """Same-category alternatives within ±20% of `reference`, with stock.

        Search rows carry no price (Productos 0.5.0), so candidates go through
        `_summarize` to get their real price and availability badge.
        """
        category_id = data.get("category_id")
        try:
            page = await self._catalog.search_products(category_id=category_id, size=10)
        except Exception:
            return []
        candidates = page.get("items", [])
        if not candidates:
            return []
        summaries = await self._summarize(candidates)
        by_id = {summary["product_id"]: summary for summary in summaries}
        enriched: list[dict] = []
        stocked: set[str] = set()
        for candidate in candidates:
            product_id = str(candidate.get("product_id", ""))
            summary = by_id.get(product_id)
            if summary is None:
                continue
            enriched.append(
                {
                    **candidate,
                    "precio_oferta": summary["price"],
                    "precio_regular": None,
                }
            )
            if summary["availability"] in ("DISPONIBLE", "POCAS_UNIDADES"):
                stocked.add(product_id)
        picks = self._similar.find(
            category_id, reference, enriched,
            available_skus=stocked or None,
            exclude={str(data.get("product_id"))},
        )
        return [
            {"product_id": str(p.get("product_id")), "name": str(p.get("name"))}
            for p in picks
        ]

    @staticmethod
    def _clarifying_question(filters) -> str:
        if filters.unresolved_category and filters.unresolved_brand:
            return (
                f"No ubico la categoría {filters.unresolved_category} ni la marca "
                f"{filters.unresolved_brand}. ¿Buscas zapatillas, polos o mochilas?"
            )
        if filters.unresolved_category:
            return (
                f"No ubico la categoría {filters.unresolved_category}. "
                "¿Buscas zapatillas, polos o mochilas?"
            )
        return (
            f"No ubico la marca {filters.unresolved_brand}. "
            "¿De qué marca lo buscas: Nike, Adidas o Andes?"
        )

    @staticmethod
    def _map_module_error(exc: Exception, *, inactive: bool = False) -> CatalogError:
        if isinstance(exc, ModuleRequestError) and exc.status == 404 and inactive:
            return CatalogError("PRODUCTO_NO_DISPONIBLE", exc.code)
        if isinstance(exc, ModuleRequestError):
            return CatalogError(exc.code, exc.detail)
        if isinstance(exc, ModuleUnavailableError):
            return CatalogError("SERVICIO_NO_DISPONIBLE", exc.module)
        return CatalogError("SERVICIO_NO_DISPONIBLE", type(exc).__name__)
