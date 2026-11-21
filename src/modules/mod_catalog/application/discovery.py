"""Conversation-scoped discovery policy over catalog and merchandising ports."""
import asyncio
import logging
import unicodedata
import uuid
from decimal import Decimal

from src.modules.mod_catalog.application.partial import PartialRows
from src.modules.mod_catalog.application.tools import SearchArguments
from src.modules.mod_catalog.domain.offer_policy import promotion_summaries
from src.modules.mod_catalog.domain.product_normalizer import ProductNormalizer
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError

logger = logging.getLogger(__name__)

# Leaves two seconds for HTTP serialization within the frontend 12 s ceiling.
OFFERS_BUDGET_SECONDS = 10.0

def _is_provider_error(exc):
    """True for upstream/provider failures; programming errors are not provider errors."""
    # Imported lazily: manage_catalog_use_case imports this module.
    from src.modules.mod_catalog.application.manage_catalog_use_case import CatalogError
    return isinstance(exc, (CatalogError, ModuleRequestError, ModuleUnavailableError, TimeoutError))


def fold(value):
    return "".join(c for c in unicodedata.normalize("NFD", value.casefold())
                   if unicodedata.category(c) != "Mn")


def block(kind, data):
    result = {"id": str(uuid.uuid4()), "tipo": kind, "datos": data}
    if kind == "ACCIONES_RAPIDAS":
        result["acciones"] = data["acciones"]  # Existing quick-action readers use this field.
    return result


ACTIVITY_CATEGORIES = {
    "running": ("Zapatillas", "Medias", "Polos"),
    "correr": ("Zapatillas", "Medias", "Polos"),
    "trotar": ("Zapatillas", "Medias", "Polos"),
    "futbol": ("Zapatillas", "Pelotas", "Medias"),
    "gym": ("Polos", "Shorts", "Zapatillas"),
    "gimnasio": ("Polos", "Shorts", "Zapatillas"),
    "trekking": ("Zapatillas", "Mochilas y morrales"),
    "padel": ("Zapatillas", "Polos"),
    "casual": ("Zapatillas", "Polos"),
    "futbol sala": ("Zapatillas", "Pelotas", "Medias"),
    "voley": ("Zapatillas", "Pelotas", "Shorts"),
    "basquet": ("Zapatillas", "Pelotas", "Shorts"),
    "tenis": ("Zapatillas", "Polos", "Shorts"),
    "natacion": ("Ropa de baño", "Accesorios"),
    "ciclismo": ("Cascos", "Guantes", "Accesorios"),
}


class DiscoveryMixin:
    async def promotions(self):
        rows = self._promotion_cache.get("promotions")
        if rows is None:
            try:
                rows = await self._catalog.list_promotions()
                promotion_summaries(rows)
            except Exception as exc:
                raise self.map_module_error(exc) from exc
            self._promotion_cache.put("promotions", rows)
        # Recheck the clock even inside the TTL.
        return promotion_summaries(rows)

    async def offers(self, args, context):
        # Independent branches retain completed data when their neighbour fails.
        offer_deadline = asyncio.get_running_loop().time() + OFFERS_BUDGET_SECONDS - 0.2

        async def products_branch():
            # Keep the discovery deadline inside the total ceiling, even with
            # PRODUCTS_TIMEOUT_SECONDS=15 and cold category/brand lookups.
            async with asyncio.timeout(OFFERS_BUDGET_SECONDS - 0.1):
                return await self.search(SearchArguments(
                    categoria=args.categoria, marca=args.marca, soloOfertas=True),
                    context, discover_offers=True, offer_deadline=offer_deadline)

        tasks = [asyncio.create_task(self.promotions()), asyncio.create_task(products_branch())]
        try:
            done, pending = await asyncio.wait(tasks, timeout=OFFERS_BUDGET_SECONDS)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            results = [task.result() if task in done and not task.exception()
                       else (task.exception() if task in done else TimeoutError()) for task in tasks]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        promotions, products = results
        for name, result in (("promotions", promotions), ("products", products)):
            if isinstance(result, Exception):
                if not _is_provider_error(result):
                    raise result
                logger.warning("offers branch %r degraded: %s: %s", name, type(result).__name__, result)
        if isinstance(promotions, Exception) and isinstance(products, Exception):
            error = next((e for e in results if getattr(e, "status", None) == 429), products)
            raise self.map_module_error(error)
        unavailable = isinstance(promotions, Exception)
        promo_rows = [] if unavailable else promotions
        headline = ("No pudimos consultar las promociones en este momento." if unavailable else
                    "Promociones vigentes." if promo_rows else "No hay campañas de promoción vigentes.")
        if isinstance(products, Exception):
            products = {"code": "OK", "texto": "No pudimos consultar los productos en oferta; puede haber más ofertas.",
                        "bloques": [], "partial": True, "complete": False,
                        "pages_scanned": 0, "total": 0, "total_scope": "scanned_pages"}
        for item in products["bloques"]:
            item["datos"]["variant"] = "ofertas"
        return {**products, "partial": unavailable or products.get("partial", False),
                "texto": f"{headline} {products['texto']}",
                "bloques": [block("LISTA_PROMOCIONES", {"promociones": promo_rows, "unavailable": unavailable}),
                            *products["bloques"]]}

    async def recommend(self, args, context):
        conversation_id = await self._owned_conversation(context)
        memory = await self._read_memory(conversation_id)
        if fold(args.actividad) == "complementos" and memory.get("crossSellLastProduct"):
            product_id = memory["crossSellLastProduct"]
            memory["crossSellSilenciado"] = False
            memory["crossSellProducts"] = [value for value in memory.get("crossSellProducts", []) if value != product_id]
            await self._save_memory(conversation_id, memory)
            return {"code": "OK", "texto": "Estos son los complementos disponibles.",
                    "bloques": await self.cross_sell(product_id, context)}
        profile = memory.get("recommendationProfile", {})
        incoming = args.model_dump(exclude_none=True, exclude_unset=True, mode="json")
        if fold(profile.get("actividad", "")) != fold(incoming["actividad"]):
            profile = {}
            memory["recommendationQuestions"] = 0
        profile = {**profile, **incoming}
        questions = memory.get("recommendationQuestions", 0)
        football = fold(profile["actividad"]) in {"futbol", "football", "soccer"}
        question = None
        if football and not profile.get("superficie"):
            question = ("superficie", "¿En qué superficie juega?", ["Césped natural", "Sintético", "Losa"])
        elif football and not profile.get("edad") and not profile.get("talla"):
            question = ("talla", "¿Qué talla usa? Puedes escribirla o ver opciones sin afinarla.", [])
        if question and questions < 2 and not profile.get("omitirPreguntas"):
            field, text, choices = question
            await self._merge_recommendation_memory(conversation_id, {
                "recommendationProfile": profile, "recommendationQuestions": questions + 1,
                "recommendationPending": True,
            })
            actions = [{"id": "RECOMENDAR_PRODUCTOS", "etiqueta": choice,
                       "payload": {**profile, field: choice}} for choice in choices]
            actions.append({"id": "RECOMENDAR_PRODUCTOS", "etiqueta": "Muéstrame opciones",
                "payload": {**profile, "omitirPreguntas": True}})
            return {"code": "OK", "texto": text,
                    "bloques": [block("ACCIONES_RAPIDAS", {"acciones": actions})]}
        summaries, normalizer = await self._recommendation_candidates(profile)
        picks = self._rank_recommendations(summaries, profile, normalizer)
        for index, row in enumerate(picks, 1):
            row["ref"] = index
            # No model-authored attributes enter reasons.
            row["recommendation_reason"] = f"{row['short_description']} · {row['price_text']}"
        await self._merge_recommendation_memory(conversation_id, {
            "recommendationProfile": profile, "recommendationQuestions": questions,
            "ultimoCarrusel": [row["product_id"] for row in picks], "recommendationPending": False,
        })
        minimum = min((row["price"] for row in summaries if row["price"] is not None
                       and row["availability"] != "AGOTADO"), default=None)
        empty_text = "No encontré productos que cumplan esos filtros y presupuesto."
        extra_blocks = []
        if not picks and minimum is not None and profile.get("presupuestoMax") is not None and minimum > Decimal(str(profile["presupuestoMax"])):
            empty_text += f" Las opciones consultadas empiezan en S/ {minimum:.2f}."
            extra_blocks = [block("ACCIONES_RAPIDAS", {"acciones": [{"id": "RECOMENDAR_PRODUCTOS",
                "etiqueta": f"Ver desde S/ {minimum:.2f}", "payload": {
                    **profile, "presupuestoMax": float(minimum), "omitirPreguntas": True}}]})]
        partial = getattr(summaries, "partial", False)
        if partial:
            empty_text += " Resultado parcial: no pudimos consultar algunos productos o datos."
        return {"code": "OK", "partial": partial, "texto": ("Resultado parcial: no pudimos consultar algunos productos o datos. " if partial else "") + "Estas opciones coinciden con tu actividad; puedes afinarlas después."
                if picks else empty_text,
                "bloques": [block("CARRUSEL_PRODUCTOS", {"variant": "recomendacion", "productos": picks, "partial": partial})] if picks else extra_blocks}

    async def _recommendation_candidates(self, profile):
        categories = await self._cached_list("categories", self._catalog.list_categories)
        brands = await self._cached_list("brands", self._catalog.list_brands)
        normalizer = ProductNormalizer(categories=categories, brands=brands)
        names = ACTIVITY_CATEGORIES.get(fold(profile["actividad"]), ())
        matched = [normalizer.normalize_category(name) for name in names]
        category_ids = list(dict.fromkeys(
            row.get("category_id") or row.get("categoria_id") for row in matched if row))
        # Unknown activities use literal catalog search, never G6 merchandising.
        candidates = []
        activity = fold(profile["actividad"])
        query = {"correr": "running", "trotar": "running", "gym": "gimnasio"}.get(activity, activity)
        pages = await asyncio.gather(*(self._catalog.search_products(category_id=category_id,
            q=query, size=10) for category_id in category_ids[:3] or [None]), return_exceptions=True)
        errors = [page for page in pages if isinstance(page, Exception)]
        rate_limit = next((error for error in errors if getattr(error, "status", None) == 429), None)
        if rate_limit is not None:
            raise self.map_module_error(rate_limit)
        if len(errors) == len(pages):
            raise self.map_module_error(errors[0])
        for page in pages:
            if isinstance(page, Exception):
                continue
            candidates.extend(page.get("items", []))
        unique = {str(row["product_id"]): row for row in candidates}
        summaries = PartialRows(partial=bool(errors))
        summary_errors = []
        for start in range(0, len(unique), 10):
            try:
                rows = await self._summarize(list(unique.values())[start:start + 10], requested_size=profile.get("talla"))
            except Exception as exc:
                summary_errors.append(exc)
                summaries.partial = True
                if getattr(exc, "status", None) == 429:
                    break
                continue
            summaries.partial = summaries.partial or rows.partial
            summaries.extend(rows)
            if rows.rate_limited:
                summaries.partial = True
                break
        if summary_errors and not summaries:
            raise self.map_module_error(summary_errors[-1])
        return summaries, normalizer

    @staticmethod
    def _rank_recommendations(summaries, profile, normalizer):
        eligible = [row for row in summaries if not row.get("missing_data") and row["availability"] != "AGOTADO"
                    and row["price"] is not None
                    and (profile.get("presupuestoMax") is None or
                         row["price"] <= Decimal(str(profile["presupuestoMax"])))]
        if profile.get("talla"):
            eligible = [row for row in eligible if any(
                str(v.get("talla")).casefold() == profile["talla"].casefold() for v in row["variants"])]
        preferred = normalizer.normalize_brand(profile["marcaPreferida"]) if profile.get("marcaPreferida") else None
        preferred_id = (preferred.get("brand_id") or preferred.get("marca_id")) if preferred else None
        def fit_score(row):
            facts = fold(row["name"] + " " + row["short_description"])
            terms = [str(profile[key]) for key in ("uso", "nivel", "destinatario", "superficie")
                     if profile.get(key)]
            if profile.get("edad") is not None:
                terms.append("niño" if profile["edad"] < 18 else "adulto")
            return sum(fold(term) in facts for term in terms)

        eligible.sort(key=lambda row: (-fit_score(row),
                      row["brand_id"] != preferred_id if preferred_id else False,
                      row["availability"] == "INDETERMINADO"))
        picks = eligible[:5]
        for alternative in eligible:
            known_brands = {row["brand_id"] for row in picks if row["brand_id"]}
            if len(known_brands) >= 2:
                break
            if alternative["brand_id"] and alternative["brand_id"] not in known_brands:
                replace_at = next((index for index in reversed(range(len(picks)))
                    if not picks[index]["brand_id"] or sum(
                        row["brand_id"] == picks[index]["brand_id"] for row in picks) > 1), None)
                if replace_at is not None:
                    picks[replace_at] = alternative
        return picks

    async def _merge_recommendation_memory(self, conversation_id, updates):
        if conversation_id is None:
            return
        memory = await self._conversation_context.get_context_for_update(conversation_id)
        memory.update(updates)
        await self._save_memory(conversation_id, memory)

    async def silence(self, args, context):
        conversation_id = await self._owned_conversation(context)
        memory = await self._conversation_context.get_context_for_update(conversation_id) if conversation_id else {}
        memory.update(crossSellSilenciado=True, crossSellPending=False)
        await self._save_memory(conversation_id, memory)
        return {"code": "OK", "texto": "No sugeriré más complementos en esta conversación.", "bloques": []}

    async def cross_sell(self, product_id, context):
        conversation_id = await self._owned_conversation(context)
        if conversation_id is None or not product_id:
            return []
        memory = await self._read_memory(conversation_id)
        seen = memory.get("crossSellProducts", [])
        if memory.get("crossSellSilenciado") or product_id in seen:
            return []
        try:
            summaries = await self._cross_sell_summaries(product_id)
        except Exception as exc:
            if not _is_provider_error(exc):
                raise
            # Optional merchandising never breaks detail or cart on provider failures.
            logger.warning("cross-sell unavailable for product %s: %s: %s", product_id, type(exc).__name__, exc)
            return []
        # Persistence errors (row lock, save, commit) are not provider failures and propagate.
        return await self._cross_sell_candidates(product_id, conversation_id, summaries)

    async def _cross_sell_summaries(self, product_id):
        candidates = await self._catalog.get_recommendations(product_id)
        ids = list(dict.fromkeys(str(row["product_id"]) for row in candidates
                   if row.get("product_id") and row["product_id"] != product_id
                   and row.get("type") == "CROSS_SELL"))[:10]
        return await self._summarize([{"product_id": value} for value in ids], positive_stock_only=True) if ids else []

    async def _cross_sell_candidates(self, product_id, conversation_id, summaries):
        picks = [row for row in summaries if not row.get("missing_data") and row["availability"] in {
            "DISPONIBLE", "POCAS_UNIDADES"} and row["price"] is not None][:3]
        # Re-read under a row lock only after all provider calls finish.
        memory = await self._conversation_context.get_context_for_update(conversation_id)
        seen = memory.get("crossSellProducts", [])
        if memory.get("crossSellSilenciado") or product_id in seen:
            await self._uow.commit()
            return []
        memory["crossSellLastProduct"] = product_id
        memory["crossSellProducts"] = [*seen, product_id]
        memory["crossSellPending"] = bool(picks)
        if picks:
            memory["ultimoCarrusel"] = [row["product_id"] for row in picks]
        await self._save_memory(conversation_id, memory)
        if not picks:
            return []
        for index, row in enumerate(picks, 1):
            row["ref"] = index
        return [block("CARRUSEL_PRODUCTOS", {"variant": "cross_sell", "titulo": "¿Te interesa complementarlo?", "productos": picks}),
                block("ACCIONES_RAPIDAS", {"acciones": [{"id": "SILENCIAR_COMPLEMENTOS", "etiqueta": "No, gracias", "payload": {}}]})]
