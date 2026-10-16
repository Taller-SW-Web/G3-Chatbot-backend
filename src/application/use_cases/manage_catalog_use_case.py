"""Catalog use case: search, detail and availability (SPEC-06, SPEC-09, SPEC-10).

Orchestrates the Productos ports, resolves references against the
conversation working memory (`ultimoCarrusel`, `filtrosVigentes`) and
returns plain data; handlers wrap it into `ToolResult` blocks. Never
imports FastAPI, httpx or SQLAlchemy.
"""
import time
import uuid
from decimal import Decimal
from typing import Any

from src.adapters.outbound.http_clients.base_client import ModuleUnavailableError
from src.application.catalog.product_lookup import (
    MEMORY_CAROUSEL_KEY,
    MEMORY_FILTERS_KEY,
    read_memory,
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

LIST_TTL_SECONDS = 600.0

_OPTION_CACHE: dict[str, tuple[float, list]] = {}


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
    ):
        self._catalog = catalog
        self._inventory = inventory
        self._conversations = conversations
        self._uow = uow
        self._normalizer = normalizer
        self._variants = VariantResolver()
        self._stock = StockValidator()
        self._similar = SimilarProducts()

    @property
    def inventory(self):
        """Inventory port for punctual availability reads (routers)."""
        return self._inventory

    # -- public orchestration -------------------------------------------------

    async def search(self, args, context) -> dict[str, Any]:
        row = await self._owned_conversation(context)
        memory = read_memory(row)
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
        await self._save_memory(row, memory)
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
        row = await self._owned_conversation(context)
        memory = read_memory(row)
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
        row = await self._owned_conversation(context)
        memory = read_memory(row)
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
        similar = await self._find_similar(data)
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
        """Owned conversation row, or None for memory-less direct reads.

        REST calls without a chat context skip the carousel memory (ordinals
        stay unavailable) but can still search by explicit ids.
        """
        conversation_id = conversation_id_from_context(context)
        if conversation_id is None:
            return None
        caller = caller_from_context(context)
        if caller is None:
            raise CatalogError("RECURSO_NO_ENCONTRADO", "missing caller")
        row = await self._conversations.find_owned(conversation_id, caller)
        if row is None:
            raise CatalogError("RECURSO_NO_ENCONTRADO", "conversation not found")
        return row

    async def _save_memory(self, row, memory: dict) -> None:
        if row is None:
            return
        try:
            row.context = dict(memory)
            await self._uow.commit()
        except BaseException:
            await self._uow.rollback()
            raise

    async def _cached_list(self, key: str, loader) -> list:
        now = time.monotonic()
        cached = _OPTION_CACHE.get(key)
        if cached is not None and now - cached[0] < LIST_TTL_SECONDS:
            return cached[1]
        values = await loader()
        _OPTION_CACHE[key] = (now, values)
        return values

    async def _summarize(
        self, items: list[dict], prefetched: dict[str, dict] | None = None
    ) -> list[dict]:
        prefetched = prefetched or {}
        summaries: list[dict] = []
        for position, item in enumerate(items, start=1):
            product_id = str(item.get("product_id", ""))
            data = prefetched.get(product_id)
            if data is None:
                try:
                    data = await self._catalog.get_product_detail(product_id)
                except Exception:
                    continue
            variants = data.get("variants") or []
            skus = [str(v.get("sku")) for v in variants if v.get("sku")]
            prices = await self._prices_by_sku(skus) if skus else {}
            statuses = await self._status_by_sku(skus) if skus else {}
            summaries.append(self._summary(data, position, prices, statuses))
        return summaries

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

    def _summary(
        self, data: dict, position: int, prices: dict, statuses: dict
    ) -> dict[str, Any]:
        variants = data.get("variants") or []
        amounts: list[Decimal] = []
        for variant in variants:
            row = prices.get(str(variant.get("sku")))
            if not row:
                continue
            offer = row.get("precio_oferta")
            amounts.append(Decimal(str(offer if offer is not None else row.get("precio_regular", 0))))
        price = min(amounts) if amounts else Decimal("0")
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

    async def _find_similar(self, data: dict) -> list[dict]:
        try:
            page = await self._catalog.search_products(
                category_id=data.get("category_id"), size=10
            )
        except Exception:
            return []
        amounts = [
            Decimal(str(item.get("precio_oferta") or item.get("precio_regular") or 0))
            for item in [data]
        ]
        reference = amounts[0] if amounts else Decimal("0")
        candidates = page.get("items", [])
        stocked: set[str] | None = None
        try:
            skus = [str(item.get("sku_base") or "") for item in candidates]
            rows = await self._inventory.get_availability([s for s in skus if s])
            if rows:
                stocked = {
                    str(row.get("sku")) for row in rows
                    if row.get("status") in ("DISPONIBLE", "STOCK_BAJO")
                }
        except Exception:
            stocked = None
        picks = self._similar.find(
            data.get("category_id"), reference, candidates,
            available_skus=stocked,
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
        from src.ports.outbound.module_errors import ModuleRequestError

        if isinstance(exc, ModuleRequestError) and exc.status == 404 and inactive:
            return CatalogError("PRODUCTO_NO_DISPONIBLE", exc.code)
        if isinstance(exc, ModuleRequestError):
            return CatalogError(exc.code, exc.detail)
        if isinstance(exc, ModuleUnavailableError):
            return CatalogError("SERVICIO_NO_DISPONIBLE", exc.module)
        return CatalogError("SERVICIO_NO_DISPONIBLE", type(exc).__name__)
