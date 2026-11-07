"""Catalog use case: search, detail and availability (SPEC-06, SPEC-09, SPEC-10).

Orchestrates the Productos ports, resolves references against the
conversation working memory (`ultimoCarrusel`, `filtrosVigentes`) and
returns plain data; handlers wrap it into `ToolResult` blocks. Never
imports FastAPI, httpx or SQLAlchemy.
"""
import asyncio
import time
import uuid
from decimal import Decimal
from typing import Any

from src.application.catalog.discovery import DiscoveryMixin, fold
from src.application.catalog.product_lookup import (
    MEMORY_CAROUSEL_KEY,
    MEMORY_FILTERS_KEY,
    resolve_product_id,
)
from src.application.tools.catalog_tools import SearchArguments
from src.application.tools.tool_context import (
    caller_from_context,
    conversation_id_from_context,
)
from src.domain.services.offer_policy import price_projection
from src.domain.services.product_normalizer import ProductNormalizer
from src.domain.services.product_projection import normalize_detail
from src.domain.services.similar_products import SimilarProducts
from src.domain.services.stock_validator import StockValidator
from src.domain.services.variant_resolver import VariantResolver, known_selection
from src.ports.outbound.module_errors import (
    ModuleRequestError,
    ModuleUnavailableError,
)

LIST_TTL_SECONDS = 600.0
MAX_VARIANTS_PER_PRODUCT = 100
# G6 bulk pricing cost grows with SKU count. Keep each request within the
# existing upstream timeout, with at most four calls in flight and no retries.
PRICE_BATCH_SIZE = 5
PRICE_CONCURRENCY = 4
INVENTORY_BATCH_SIZE = 20
COMMERCIAL_BUDGET_SECONDS = 8.0
OFFER_DISCOVERY_PAGES = 5
OFFER_DISCOVERY_SECONDS = 8.0


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
    def __init__(self, code: str, detail: str = "", *, status=None, retry_after=None):
        self.code = code
        self.detail = detail
        self.status = status
        self.retry_after = retry_after
        super().__init__(detail or code)


class ManageCatalogUseCase(DiscoveryMixin):
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
        self._promotion_cache = OptionCache(60)

    @property
    def inventory(self):
        """Inventory port for punctual availability reads (routers)."""
        return self._inventory

    # -- public orchestration -------------------------------------------------

    async def search(self, args, context, *, discover_offers: bool = False) -> dict[str, Any]:
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
        if discover_offers:
            summaries, total, exhausted, pages_scanned = await self._discover_offer_products(args, filters)
            scope = "catalog" if exhausted else "scanned_pages"
            text = (f"Encontré {total} productos en oferta en el catálogo con esos filtros."
                    if exhausted else
                    f"Encontré {total} productos en oferta en las páginas consultadas. "
                    "La búsqueda es parcial; puede haber más ofertas en el catálogo.")
            if total > len(summaries):
                text += f" Te muestro {len(summaries)} de esos productos."
        else:
            page = await self._search_page(args, filters, use=args.uso)
            if args.uso and not page.get("items"):
                # `uso` is a soft hint: never let it alone empty the results.
                page = await self._search_page(args, filters)
            items = page.get("items", [])
            if not items:
                return {
                    "code": "OK",
                    "total": 0,
                    "total_scope": "page" if args.soloOfertas or args.talla or args.color else "catalog",
                    "texto": (
                        "No encontré productos con esos filtros. "
                        "Prueba quitando un filtro o buscando por categoría."
                    ),
                    "bloques": [],
                }
            local_filter = args.talla is not None or args.color is not None
            summaries = await self._summarize(
                items[:10], requested_size=args.talla, requested_color=args.color,
            )
            if local_filter:
                summaries = [row for row in summaries if row["variants"]]
            if args.soloOfertas:
                summaries = [row for row in summaries if row["offer_price"] is not None]
            total = len(summaries) if args.soloOfertas or local_filter else page.get("total", len(summaries))
            scope = "page" if args.soloOfertas or local_filter else "catalog"
            text = (f"Encontré {total} productos en esta página." if scope == "page" else
                    f"Encontré {total} productos. Te muestro los primeros:")
        for index, row in enumerate(summaries, 1):
            row["ref"] = index
        memory[MEMORY_CAROUSEL_KEY] = [s["product_id"] for s in summaries]
        memory[MEMORY_FILTERS_KEY] = {
            "q": filters.q,
            "category_id": filters.category_id,
            "brand_id": filters.brand_id,
        }
        await self._save_memory(conversation_id, memory)
        return {
            "code": "OK",
            "texto": text,
            "bloques": [{
                "id": uuid.uuid4().hex,
                "tipo": "CARRUSEL_PRODUCTOS",
                "datos": {"productos": summaries},
            }],
            "total": total,
            "total_scope": scope,
            **({"complete": exhausted, "pages_scanned": pages_scanned} if discover_offers else {}),
        }

    async def _search_page(self, args, filters, *, use: str | None = None) -> dict[str, Any]:
        query = " ".join(part for part in (filters.q, use) if part) or None
        return await self._catalog.search_products(
            q=query,
            category_id=args.categoriaId or filters.category_id,
            brand_id=args.marcaId or filters.brand_id,
            channel=args.canal,
            page=args.pagina,
            size=args.tamanio,
            state=args.estado,
            min_price=args.precioMin,
            max_price=args.precioMax,
        )

    async def _discover_offer_products(self, args, filters):
        """Scan bounded pages only for chat discovery; REST retains upstream pagination.

        At most 50 unique products, ten concurrent detail reads per page and the
        existing per-product commercial caps. Never suppress supplier failures.
        """
        summaries = []
        seen = set()
        exhausted = False
        pages_scanned = 0
        deadline = asyncio.timeout(OFFER_DISCOVERY_SECONDS)
        try:
            async with deadline:
                for number in range(1, OFFER_DISCOVERY_PAGES + 1):
                    page = await self._catalog.search_products(
                        q=filters.q, category_id=args.categoriaId or filters.category_id,
                        brand_id=args.marcaId or filters.brand_id, channel=args.canal,
                        page=number, size=10, state=args.estado,
                        min_price=args.precioMin, max_price=args.precioMax,
                    )
                    items = page.get("items", [])
                    unique = []
                    for item in items[:10]:
                        pid = str(item["product_id"])
                        if pid not in seen:
                            seen.add(pid)
                            unique.append(item)
                    rows = await self._summarize(unique, requested_size=args.talla, requested_color=args.color)
                    summaries.extend(row for row in rows if row["offer_price"] is not None
                                     and (not (args.talla or args.color) or row["variants"]))
                    pages_scanned += 1
                    total = page.get("total")
                    exhausted = not items or (isinstance(total, int) and number * 10 >= total)
                    if exhausted or len(summaries) >= args.tamanio:
                        break
        except TimeoutError as exc:
            if not deadline.expired():
                raise self.map_module_error(exc) from exc
            # Only completed, validated pages are counted. Unknown is never no-offers.
            pass
        except Exception as exc:
            raise self.map_module_error(exc) from exc
        # Count every validated match, including those beyond the carousel size.
        return summaries[:args.tamanio], len(summaries), exhausted, pages_scanned

    async def detail(self, args, context) -> dict[str, Any]:
        conversation_id = await self._owned_conversation(context)
        memory = await self._read_memory(conversation_id)
        product_id = resolve_product_id(
            memory, producto_ref=args.productoRef, product_id=args.product_id
        )
        if product_id is None and args.nombre:
            product_id, answer = await self._resolve_by_name(args.nombre, context)
            if product_id is None:
                return answer
        if product_id is None:
            return {
                "code": "OK",
                "texto": "¿De qué producto quieres el detalle? Dime el número del carrusel.",
                "bloques": [],
            }
        try:
            data = await self._catalog.get_product_detail(product_id)
        except Exception as exc:
            raise self.map_module_error(exc, inactive=True) from exc
        summary = (await self._summarize([data], prefetched={product_id: data}))[0]
        try:
            extras = await self.cross_sell(product_id, context)
        except Exception:
            extras = []  # Merchandising cannot break a successful detail read.
        return {
            "code": "OK",
            "texto": f"{summary['name']}: {summary['price_text']}",
            "bloques": [{
                "id": uuid.uuid4().hex,
                "tipo": "DETALLE_PRODUCTO",
                "datos": {"producto": summary},
            }, *extras],
        }

    async def availability(self, args, context) -> dict[str, Any]:
        conversation_id = await self._owned_conversation(context)
        memory = await self._read_memory(conversation_id)
        product_id = resolve_product_id(
            memory, producto_ref=args.productoRef, product_id=args.product_id
        )
        if product_id is None and args.nombre:
            product_id, answer = await self._resolve_by_name(args.nombre, context)
            if product_id is None:
                return answer
        if product_id is None:
            return {
                "code": "OK",
                "texto": "¿La disponibilidad de qué producto consulto?",
                "bloques": [],
            }
        try:
            data = await self._catalog.get_product_detail(product_id)
        except Exception as exc:
            raise self.map_module_error(exc, inactive=True) from exc
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
                            "seleccion": known_selection(
                                getattr(args, "talla", None),
                                getattr(args, "color", None),
                            ),
                        },
                    }],
                }
            return {
                "code": "OK",
                "texto": "Esa combinación no existe. Te muestro alternativas cercanas.",
                "bloques": [],
            }
        statuses = await self._status_by_sku([match.sku])
        if statuses[match.sku] == "INDETERMINADO":
            return {"code": "OK", "texto": "No pudimos confirmar el stock.", "bloques": []}
        check = self._stock.check_row({"sku": match.sku, "status": statuses[match.sku]})
        if check.ok and not check.warning:
            return {"code": "OK", "texto": "Sí, hay stock disponible.", "bloques": []}
        if check.ok:
            return {
                "code": "OK",
                "texto": "Hay pocas unidades. Si te interesa, agrégalo pronto.",
                "bloques": [],
            }
        reference = await self._reference_price({**data, "variants": [{"sku": match.sku}]})
        similar = await self._find_similar(data, reference) if reference is not None else []
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

    async def _resolve_by_name(self, name: str, context) -> tuple[str | None, dict[str, Any]]:
        """Product id for a product name, or a ready answer when it is missing or ambiguous.

        Exact (accent-insensitive) name wins; otherwise a single candidate whose name is
        contained in the request (or the reverse); otherwise a short carousel to choose from.
        """
        try:
            page = await self._catalog.search_products(q=name, size=5)
        except Exception as exc:
            raise self.map_module_error(exc) from exc
        items = page.get("items", [])
        if not items:
            return None, {
                "code": "OK",
                "texto": f"No encontré un producto llamado {name}. ¿Puedes darme más detalle?",
                "bloques": [],
            }
        wanted = fold(name).strip()
        exact = [row for row in items if fold(str(row.get("name", ""))).strip() == wanted]
        related = [row for row in items if wanted and (
            fold(str(row.get("name", ""))) in wanted or wanted in fold(str(row.get("name", ""))))]
        for candidates in (exact, related, items):
            if len(candidates) == 1:
                return str(candidates[0]["product_id"]), {}
        choices = await self.search(SearchArguments(q=name, tamanio=5), context)
        choices["texto"] = f"Encontré varios productos parecidos a {name}. ¿Cuál te interesa?"
        return None, choices

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
        self, items: list[dict], prefetched: dict[str, dict] | None = None,
        *, positive_stock_only: bool = False, requested_size: str | None = None,
        requested_color: str | None = None,
    ) -> list[dict]:
        """Summaries for up to 10 carousel items, batching the fan-out.

        Details are fetched concurrently; prices use bounded small batches and
        availability uses bulk reads. Every selected SKU is inspected.
        """
        prefetched = prefetched or {}
        resolved: list[tuple[int, dict]] = []
        missing: list[tuple[int, str]] = []
        for position, item in enumerate(items[:10], start=1):
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
            for (position, _), detail in zip(missing, fetched, strict=True):
                if isinstance(detail, ModuleRequestError) and detail.status == 404:
                    continue
                if isinstance(detail, Exception):
                    raise self.map_module_error(detail)
                if not isinstance(detail, dict):
                    # Sin detalle (None) no hay nada que mostrar: se omite como el 404.
                    continue
                resolved.append((position, detail))
        resolved.sort(key=lambda entry: entry[0])
        # Bound commercial fan-out, selecting requested attributes before the cap.
        resolved = [
            (position, {**data, "variants": [
                variant for variant in normalize_detail(data)["variants"]
                if (requested_size is None or str(variant.get("talla")).casefold() == requested_size.casefold())
                and (requested_color is None or str(variant.get("color")).casefold() == requested_color.casefold())
            ][:MAX_VARIANTS_PER_PRODUCT]})
            for position, data in resolved
        ]
        skus = [
            str(variant.get("sku"))
            for _, data in resolved
            for variant in (data.get("variants") or [])
            if variant.get("sku")
        ]
        prices, statuses = await asyncio.gather(
            self._prices_by_sku(skus), self._status_by_sku(skus)
        ) if skus else ({}, {})
        return [
            self._summary(data, position, prices, statuses, positive_stock_only=positive_stock_only, requested_size=requested_size, requested_color=requested_color)
            for position, data in resolved
        ]

    async def _prices_by_sku(self, skus: list[str]) -> dict[str, dict]:
        rows = []
        clean = list(dict.fromkeys(skus))
        try:
            async with asyncio.timeout(COMMERCIAL_BUDGET_SECONDS):
                for start in range(0, len(clean), PRICE_BATCH_SIZE * PRICE_CONCURRENCY):
                    window = clean[start:start + PRICE_BATCH_SIZE * PRICE_CONCURRENCY]
                    results = await asyncio.gather(*(
                        self._catalog.get_prices(window[offset:offset + PRICE_BATCH_SIZE])
                        for offset in range(0, len(window), PRICE_BATCH_SIZE)
                    ), return_exceptions=True)
                    # Finish this bounded wave, then stop on an upstream error;
                    # never return partial prices or launch the next wave on 429.
                    for result in results:
                        if isinstance(result, BaseException):
                            raise result
                        rows.extend(result)
        except Exception as exc:
            raise self.map_module_error(exc) from exc
        return {str(row.get("sku")): row for row in rows if row.get("sku")}

    async def _status_by_sku(self, skus: list[str]) -> dict[str, str]:
        clean = list(dict.fromkeys(skus))
        rows = []
        try:
            async with asyncio.timeout(COMMERCIAL_BUDGET_SECONDS):
                for start in range(0, len(clean), INVENTORY_BATCH_SIZE):
                    rows.extend(await self._inventory.get_availability(clean[start:start + INVENTORY_BATCH_SIZE]))
            statuses = {str(row["sku"]): row["status"] for row in rows}
            if any(statuses.get(sku) not in {
                "DISPONIBLE", "STOCK_BAJO", "AGOTADO", "INDETERMINADO"
            } for sku in clean):
                raise ValueError("missing or incompatible availability")
            return statuses
        except Exception as exc:
            raise self.map_module_error(exc) from exc

    @staticmethod
    def _price_from_variants(variants: list, prices: dict) -> Decimal | None:
        amounts = [price_projection(prices[str(v["sku"])])["price"]
                   for v in variants if str(v.get("sku")) in prices]
        return min((value for value in amounts if value is not None), default=None)

    def _summary(
        self, data: dict, position: int, prices: dict, statuses: dict,
        *, positive_stock_only: bool = False, requested_size: str | None = None,
        requested_color: str | None = None,
    ) -> dict[str, Any]:
        data = normalize_detail(data)
        variants = data["variants"]
        if requested_size is not None:
            variants = [v for v in variants if str(v.get("talla")).casefold() == requested_size.casefold()]
        if requested_color is not None:
            variants = [v for v in variants if str(v.get("color")).casefold() == requested_color.casefold()]
        pricing_variants = [v for v in variants if statuses.get(str(v.get("sku"))) in {
            "DISPONIBLE", "STOCK_BAJO"}] if positive_stock_only else [
                v for v in variants if statuses.get(str(v.get("sku"))) != "AGOTADO"]
        if not pricing_variants and not positive_stock_only:
            pricing_variants = variants
        try:
            projections = [price_projection(prices[str(v["sku"])])
                           for v in pricing_variants if str(v.get("sku")) in prices]
            projection = min((p for p in projections if p["price"] is not None),
                             key=lambda p: p["price"], default=price_projection({}))
        except ValueError as exc:
            raise self.map_module_error(exc) from exc
        price = projection["price"]
        name = str(data.get("name", ""))
        description = data.get("description") or name
        images = data.get("images") or []
        return {
            "ref": position,
            "product_id": str(data.get("product_id", "")),
            "name": name,
            "brand": data.get("brand_name"),
            "brand_id": data.get("brand_id"),
            "image": images[0] if images else None,
            **projection,
            "price_text": f"S/ {price:.2f}" if price is not None else "Precio no disponible",
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
            return "INDETERMINADO"
        values = {statuses.get(str(v.get("sku")), "") for v in variants if v.get("sku")}
        if "DISPONIBLE" in values:
            return "DISPONIBLE"
        if "STOCK_BAJO" in values:
            return "POCAS_UNIDADES"
        if "AGOTADO" in values and len(values) == 1:
            return "AGOTADO"
        return "INDETERMINADO"

    async def _reference_price(self, data: dict) -> Decimal | None:
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
        category_id = normalize_detail(data).get("category_id")
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
                    **normalize_detail(candidate),
                    "precio_oferta": summary["price"],
                    "precio_regular": None,
                }
            )
            if summary["availability"] in ("DISPONIBLE", "POCAS_UNIDADES"):
                stocked.add(product_id)
        picks = self._similar.find(
            category_id, reference, enriched,
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
    def map_module_error(exc: Exception, *, inactive: bool = False) -> CatalogError:
        if isinstance(exc, CatalogError):
            return exc
        if isinstance(exc, ModuleRequestError) and exc.status == 404 and inactive:
            return CatalogError("PRODUCTO_NO_DISPONIBLE", exc.code)
        if isinstance(exc, ModuleRequestError):
            return CatalogError(exc.code, exc.detail, status=exc.status,
                                retry_after=getattr(exc, "retry_after", None))
        if isinstance(exc, ModuleUnavailableError):
            return CatalogError("SERVICIO_NO_DISPONIBLE", exc.module,
                                retry_after=getattr(exc, "retry_after", None))
        return CatalogError("SERVICIO_NO_DISPONIBLE", type(exc).__name__)
