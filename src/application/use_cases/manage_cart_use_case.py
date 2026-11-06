"""Cart use case: add, change, remove, clear and view (SPEC-11, SPEC-10).

Owns cart lifecycle (anonymous per conversation with 7-day expiry,
single ACTIVE cart per customer), live stock checks, live totals and
TOOL audit rows. Applying coupons (SPEC-13) and quoting shipping
(SPEC-12) are out of scope: the stored coupon is only revalidated
through the promotions evaluation, and shipping stays empty until
checkout quotes it. Never imports FastAPI, httpx or SQLAlchemy: the
repositories (`CartRepository`, `CartItemRepository`) build the rows and
translate driver errors into `StaleCartError`/`CartConflictError`.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from src.application.catalog.product_lookup import (
    resolve_product_id,
)
from src.application.services.coupon_attempts import CouponAttempts
from src.application.tools.tool_registry import ExecutionContext
from src.domain.entities.message import MessageRole
from src.domain.services.cart_line_resolver import CartLineResolver
from src.domain.services.offer_policy import amount, instant, price_projection
from src.domain.services.product_projection import normalize_detail
from src.domain.services.stock_validator import StockValidator
from src.domain.services.totals_calculator import PricedLine, TotalsCalculator
from src.domain.services.variant_resolver import VariantResolver
from src.ports.outbound.cart_repository_port import (
    CartConflictError,
    StaleCartError,
)
from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError

logger = logging.getLogger(__name__)

MAX_PER_LINE = 10
MAX_LINES = 20
ANONYMOUS_TTL = timedelta(days=7)

# Compartido por todas las peticiones: un `ManageCartUseCase` se construye por
# request, así que el contador de intentos de cupón vive a nivel de módulo.
_DEFAULT_COUPON_ATTEMPTS = CouponAttempts()
_COUPON_SIN_CAMBIAR = object()


class CartError(Exception):
    def __init__(self, code: str, detail: str = "", data: dict | None = None):
        self.code = code
        self.detail = detail
        self.data = data or {}
        super().__init__(detail or code)


class ManageCartUseCase:
    def __init__(
        self,
        carts,
        items,
        catalog,
        inventory,
        conversations,
        messages,
        uow,
        totals: TotalsCalculator | None = None,
        discovery=None,
        coupon_attempts: CouponAttempts | None = None,
    ):
        self._carts = carts
        self._items = items
        self._catalog = catalog
        self._inventory = inventory
        self._conversations = conversations
        self._messages = messages
        self._uow = uow
        self._totals = totals or TotalsCalculator()
        self._coupon_attempts = coupon_attempts or _DEFAULT_COUPON_ATTEMPTS
        self._lines = CartLineResolver()
        self._variants = VariantResolver()
        self._stock = StockValidator()
        self._discovery = discovery

    # -- public operations ----------------------------------------------------

    async def add(self, caller, conversation_id: uuid.UUID, *, sku=None,
                  producto_ref=None, product_id=None, size=None, color=None,
                  quantity: int = 1, tool: str = "agregar_al_carrito", deliver_cross_sell: bool = True) -> dict:
        if not 1 <= quantity <= MAX_PER_LINE:
            raise CartError("LIMITE_CANTIDAD", f"quantity {quantity} out of 1..10")
        added = []
        result = await self._mutate(
            caller, conversation_id, tool,
            lambda cart: self._add_line(cart, conversation_id, sku, producto_ref,
                                        product_id, size, color, quantity, added),
        )
        if deliver_cross_sell and added and added[-1] and self._discovery is not None and not result.get("requiere_variante"):
            context = ExecutionContext(
                conversacion_id=str(conversation_id) if conversation_id else None,
                cliente_id=str(caller.customer_id) if caller.customer_id else None,
                chat_sid=caller.anonymous_sid)
            try:
                result["bloques"].extend(await self._discovery.cross_sell(added[-1], context))
            except Exception:
                # The cart transaction has already committed. No failure here may undo it.
                await self._uow.rollback()
                logger.warning("cross-sell unavailable after successful add")
        return result

    async def update_quantity(self, caller, conversation_id: uuid.UUID, *,
                              item_id=None, item_ref=None, name=None,
                              quantity: int = 0, tool: str = "cambiar_cantidad") -> dict:
        return await self._mutate(
            caller, conversation_id, tool,
            lambda cart: self._set_quantity(cart, item_id, item_ref, name, quantity),
        )

    async def remove_item(self, caller, conversation_id: uuid.UUID, *,
                          item_id=None, item_ref=None, name=None,
                          tool: str = "quitar_del_carrito") -> dict:
        return await self._mutate(
            caller, conversation_id, tool,
            lambda cart: self._drop_line(cart, item_id, item_ref, name),
        )

    async def clear(self, caller, conversation_id: uuid.UUID,
                    tool: str = "vaciar_carrito") -> dict:
        async def _clear(cart):
            for row in await self._items.listar_por_cart(cart.id):
                await self._items.eliminar(row.id)
            await self._carts.reset_checkout(cart.id)
            return "Vaciamos tu carrito."
        return await self._mutate(caller, conversation_id, tool, _clear)

    async def view(self, caller, conversation_id: uuid.UUID) -> dict:
        cart = await self._open_cart(caller, conversation_id, create=False)
        if cart is None:
            return {"code": "OK", "texto": "Tu carrito está vacío", "bloques": []}
        return await self._cart_view(cart, conversation_id)

    async def apply_coupon(
        self,
        caller,
        conversation_id: uuid.UUID,
        *,
        codigo: str,
        confirmar_reemplazo: bool = False,
    ) -> dict:
        """Aplica un cupón validado contra Productos, sin consumirlo (SPEC-13).

        El uso se consume recién cuando Ventas confirma el pedido. Un carrito
        admite un solo cupón: reemplazarlo exige `confirmar_reemplazo`.
        """
        customer_id = self._require_customer(caller)
        espera = await self._coupon_attempts.blocked(customer_id)
        if espera is not None:
            raise CartError(
                "DEMASIADAS_SOLICITUDES",
                "Demasiados intentos de cupón. Intenta de nuevo más tarde.",
                {"reintentarEn": espera},
            )
        codigo = (codigo or "").strip().upper()
        if not codigo:
            raise CartError("VALIDACION", "El código del cupón es obligatorio.")
        cart = await self._open_cart(caller, conversation_id, create=False)
        if cart is None:
            raise CartError("RECURSO_NO_ENCONTRADO", "cart not found")
        if cart.coupon_code and cart.coupon_code != codigo and not confirmar_reemplazo:
            raise CartError(
                "REQUIERE_CONFIRMACION_REEMPLAZO",
                "Tu carrito ya tiene un cupón aplicado.",
                {"cuponActual": cart.coupon_code, "cuponNuevo": codigo},
            )
        rows = await self._items.listar_por_cart(cart.id)
        if not rows:
            raise CartError("VALIDACION", "Agrega productos antes de aplicar un cupón.")
        try:
            evaluacion = await self._catalog.evaluate_promotions(
                [{"sku": row.sku, "quantity": row.quantity} for row in rows],
                coupon_code=codigo,
                customer_ref=customer_id,
            )
        except Exception as exc:
            raise self._map_module_error(exc) from exc
        if not evaluacion.get("coupon_applied", False):
            await self._coupon_attempts.record_failure(customer_id)
            motivo = str(
                evaluacion.get("reason") or evaluacion.get("motivo") or "INEXISTENTE"
            )
            raise CartError("CUPON_INVALIDO", "El cupón no es válido.", {"motivo": motivo})
        await self._coupon_attempts.clear(customer_id)
        await self._carts.set_coupon(cart.id, codigo)
        try:
            view = await self._cart_view(cart, conversation_id, coupon_code=codigo)
            await self._uow.commit()
        except BaseException:
            await self._uow.rollback()
            raise
        return {
            "code": "OK",
            "texto": f"Cupón {codigo} aplicado. {view['texto']}",
            "cupon": {
                "codigo": codigo,
                "descuento": float(evaluacion.get("discount_amount") or 0),
                "moneda": "PEN",
                "promocionId": evaluacion.get("promotion_id"),
            },
            "bloques": view["bloques"],
        }

    async def remove_coupon(self, caller, conversation_id: uuid.UUID) -> dict:
        """Quita el cupón del carrito y recalcula los totales (SPEC-13)."""
        self._require_customer(caller)
        cart = await self._open_cart(caller, conversation_id, create=False)
        if cart is None:
            raise CartError("RECURSO_NO_ENCONTRADO", "cart not found")
        await self._carts.set_coupon(cart.id, None)
        try:
            view = await self._cart_view(cart, conversation_id, coupon_code=None)
            await self._uow.commit()
        except BaseException:
            await self._uow.rollback()
            raise
        return {
            "code": "OK",
            "texto": "Quitamos el cupón de tu carrito.",
            "cupon": None,
            "bloques": view["bloques"],
        }

    @staticmethod
    def _require_customer(caller) -> str:
        if caller is None or caller.customer_id is None:
            raise CartError(
                "REQUIERE_SESION", "Se requiere una sesión de cliente para usar cupones."
            )
        return str(caller.customer_id)

    async def validate_cart(self, cart) -> dict[str, dict]:
        """Per-line live revalidation for checkout (SPEC-10 Req. 2)."""
        rows = await self._items.listar_por_cart(cart.id)
        skus = [row.sku for row in rows]
        if not skus:
            return {}
        statuses = {
            row.get("sku"): row for row in
            await self._inventory.get_availability(skus)
        }
        lines = [{"sku": row.sku, "quantity": row.quantity} for row in rows]
        verdicts = {
            v.get("sku"): v for v in
            await self._inventory.verify_quantities(lines)
        }
        outcome: dict[str, dict] = {}
        for row in rows:
            projection = self._stock.check_row(statuses.get(row.sku))
            quantity = self._stock.check_quantity(
                (verdicts.get(row.sku) or {}).get("veredicto")
            )
            outcome[row.sku] = {
                "projection": projection.code,
                "quantity": quantity.code,
                "ok": projection.ok and quantity.ok,
            }
        return outcome

    async def mark_converted(self, cart_id: uuid.UUID) -> None:
        """Hook for the order flow (SPEC-15): a paid cart becomes CONVERTED."""
        try:
            await self._carts.cambiar_estado(cart_id, "CONVERTED")
            await self._uow.commit()
        except BaseException:
            await self._uow.rollback()
            raise

    # -- mutation frame -------------------------------------------------------

    async def _mutate(self, caller, conversation_id, tool, operation) -> dict:
        attempts = 0
        while True:
            attempts += 1
            try:
                cart = await self._open_cart(caller, conversation_id, create=True)
                texto = await operation(cart)
                view = await self._cart_view(cart, conversation_id, touch=False)
                if conversation_id is not None:
                    await self._audit(conversation_id, tool, view["code"], texto)
                await self._uow.commit()
                view["texto"] = self._with_summary(texto, view)
                return view
            except StaleCartError:
                await self._uow.rollback()
                if attempts >= 2:
                    raise CartError("CONFLICTO", "concurrent modification, retry")
            except CartConflictError as exc:
                await self._uow.rollback()
                raise CartError("CONFLICTO", "cart was modified concurrently") from exc
            except CartError as exc:
                await self._uow.rollback()
                if exc.code == "VARIANTE_REQUERIDA":
                    # The REST layer turns this into 409 problem+json; the
                    # tool layer keeps the SELECTOR_VARIANTE block for the UI.
                    return {
                        "code": "OK",
                        "texto": exc.detail,
                        "requiere_variante": True,
                        "faltantes": list(exc.data.get("faltantes", [])),
                        "product_id": exc.data.get("product_id"),
                        "bloques": [{
                            "id": uuid.uuid4().hex,
                            "tipo": "SELECTOR_VARIANTE",
                            "datos": exc.data,
                        }],
                    }
                raise
            except Exception as exc:
                await self._uow.rollback()
                logger.exception("cart mutation %s failed", tool)
                raise self._map_module_error(exc) from exc

    @staticmethod
    def _with_summary(texto: str, view: dict) -> str:
        bloques = view.get("bloques") or []
        if not bloques or bloques[0].get("tipo") != "CARRITO":
            return texto
        datos = bloques[0].get("datos", {})
        lineas = datos.get("lineas", [])
        return f"{texto} Tu carrito: {len(lineas)} productos · S/ {datos.get('total', 0):.2f}"

    async def _add_line(self, cart, conversation_id, sku, producto_ref,
                        product_id, size, color, quantity, added=None) -> str:
        resolved = await self._resolve_sku(
            cart, conversation_id, sku, producto_ref, product_id, size, color
        )
        if resolved is None:
            raise CartError(
                "PRODUCTO_NO_DISPONIBLE", "could not resolve a sellable SKU"
            )
        sku_value, detail, variant = resolved
        if sku_value is None:
            if isinstance(variant, dict) and variant.get("faltantes"):
                raise CartError(
                    "VARIANTE_REQUERIDA",
                    f"¿En qué {' y '.join(variant['faltantes'])} lo buscas?",
                    {"product_id": detail.get("product_id"),
                     "faltantes": variant["faltantes"]},
                )
            raise CartError(
                "PRODUCTO_NO_DISPONIBLE", "could not resolve a sellable SKU"
            )
        existing = await self._items.obtener_por_cart_y_sku(cart.id, sku_value)
        current = existing.quantity if existing else 0
        if current + quantity > MAX_PER_LINE:
            raise CartError(
                "LIMITE_CANTIDAD",
                "El máximo por producto es 10 unidades",
                {"max_agregable": MAX_PER_LINE - current},
            )
        if existing is None and await self._line_count(cart) >= MAX_LINES:
            raise CartError(
                "LIMITE_CANTIDAD", "Tu carrito llegó al máximo de 20 productos distintos"
            )
        await self._check_can_sell(sku_value, current + quantity)
        captured_price = []
        price = await self._live_price(sku_value, captured_price)
        if conversation_id is not None and captured_price:
            projection = price_projection(captured_price[0])
            memory = await self._memory(conversation_id)
            snapshots = dict(memory.get("cartOfferPrices", {}))
            if projection["offer_price"] is not None:
                snapshots[sku_value] = {"name": detail.get("name"), **projection}
                # Context must stay JSON-serializable in the existing repository.
                snapshots[sku_value]["price"] = str(projection["price"])
                memory["cartOfferPrices"] = snapshots
                await self._conversations.save_context(conversation_id, memory)
        if existing is None:
            await self._items.agregar(
                cart_id=cart.id,
                product_id=str(detail.get("product_id") or ""),
                sku=sku_value,
                name=str(detail.get("name") or sku_value),
                variant_description=self._variant_label(variant),
                image_url=(detail.get("images") or [None])[0],
                quantity=quantity,
                unit_price_ref=float(price),
            )
        else:
            await self._items.set_quantity(
                existing.id, quantity=current + quantity, unit_price_ref=float(price)
            )
        if added is not None:
            added.append(str(detail.get("product_id")))
        prefijo = f"Agregué {detail.get('name') or sku_value} (S/ {float(price):.2f})."
        return f"{prefijo}{self._variant_suffix(variant)}"

    async def _set_quantity(self, cart, item_id, item_ref, name, quantity) -> str:
        row = await self._resolve_line(cart, item_id, item_ref, name)
        if quantity == 0:
            await self._items.eliminar(row.id)
            return f"Quité {row.name} del carrito."
        if not 1 <= quantity <= MAX_PER_LINE:
            raise CartError("LIMITE_CANTIDAD", "El máximo por producto es 10 unidades")
        await self._check_can_sell(row.sku, quantity)
        await self._items.set_quantity(
            row.id, quantity=quantity, unit_price_ref=float(await self._live_price(row.sku))
        )
        return f"Cambié {row.name} a {quantity}."

    async def _drop_line(self, cart, item_id, item_ref, name) -> str:
        row = await self._resolve_line(cart, item_id, item_ref, name)
        await self._items.eliminar(row.id)
        return f"Quité {row.name} del carrito."

    # -- reads ----------------------------------------------------------------

    async def _cart_view(
        self, cart, conversation_id, *, touch=True, coupon_code=_COUPON_SIN_CAMBIAR
    ) -> dict:
        rows = await self._items.listar_por_cart(cart.id)
        if not rows:
            return {"code": "OK", "texto": "Tu carrito está vacío", "bloques": []}
        skus = [row.sku for row in rows]
        price_rows = {}
        prices, (evaluation, promotions_ok) = await asyncio.gather(
            self._prices_map(skus, price_rows),
            self._evaluate(cart, rows, coupon_code=coupon_code),
        )
        # No campaign lookup is needed when no campaign was applied. Preserve
        # every authoritative evaluated discount; only its display name is optional.
        names = await self._promotion_names() if evaluation and evaluation.get("promotion_id") else {}
        codigo_efectivo = (
            cart.coupon_code if coupon_code is _COUPON_SIN_CAMBIAR else coupon_code
        )
        if codigo_efectivo and evaluation and evaluation.get("promotion_id"):
            names[str(evaluation["promotion_id"])] = f"Cupón {codigo_efectivo}"
        priced: list[PricedLine] = []
        for row in rows:
            price = prices.get(row.sku)
            priced.append(PricedLine(
                sku=row.sku,
                name=row.name,
                quantity=row.quantity,
                unit_price=price if price is not None else Decimal(str(row.unit_price_ref)),
                previous_price=Decimal(str(row.unit_price_ref)),
                image_url=row.image_url,
                variant_description=row.variant_description,
                available=price is not None,
            ))
        totals = self._totals.calculate(
            priced, evaluation=evaluation, promotion_names=names
        )
        await self._carts.set_totals(cart.id, {
            "subtotal": str(totals.subtotal),
            "descuentos": [
                {"name": d["name"], "amount": str(d["amount"])} for d in totals.discounts
            ],
            "total": str(totals.total),
        })
        if touch:
            await self._carts.touch(cart.id)
            if conversation_id is not None:
                await self._touch(conversation_id)
        lines_out = [
            {
                "item_id": str(row.id),
                "sku": row.sku,
                "nombre": row.name,
                "variante": row.variant_description,
                "cantidad": row.quantity,
                "precio": float(priced[i].unit_price) if priced[i].available else None,
                "disponible": priced[i].available,
                "imagen": row.image_url,
            }
            for i, row in enumerate(rows)
        ]
        unavailable = [line for i, line in enumerate(lines_out) if not priced[i].available]
        texto = f"Tu carrito: {len(rows)} productos · S/ {totals.total:.2f}"
        notices = list(totals.notices)
        if conversation_id is not None:
            memory = await self._memory(conversation_id)
            snapshots = dict(memory.get("cartOfferPrices", {}))
            changed = False
            for row in rows:
                prior = snapshots.get(row.sku, {})
                expiry = prior.get("offer_valid_until")
                if (expiry and instant(expiry) <= datetime.now(timezone.utc)
                        and prices.get(row.sku) is not None
                        and price_projection(price_rows.get(row.sku, {}))["offer_price"] is None):
                    notices.append(f"La oferta de {row.name} terminó; el precio ahora es S/ {prices[row.sku]:.2f}")
                    snapshots.pop(row.sku)
                    changed = True
            if changed:
                memory["cartOfferPrices"] = snapshots
                await self._conversations.save_context(conversation_id, memory)
        for line in unavailable:
            notices.append(f"{line['nombre']} ya no está disponible. Puedes quitarlo.")
            line["nota"] = "Ya no disponible"
        if not promotions_ok:
            notices.append("El checkout queda bloqueado hasta calcular promociones.")
        return {
            "code": "OK",
            "texto": texto,
            "bloques": [{
                "id": uuid.uuid4().hex,
                "tipo": "CARRITO",
                "datos": {
                    "lineas": lines_out,
                    "subtotal": float(totals.subtotal),
                    "descuentos": [
                        {"nombre": d["name"], "monto": float(d["amount"])}
                        for d in totals.discounts
                    ],
                    "costoEnvio": None,
                    "envio": "se calcula al elegir la dirección",
                    "total": float(totals.total),
                    "avisos": notices,
                    "promociones_ok": promotions_ok,
                    "promotion_reason": evaluation.get("reason") if evaluation else None,
                },
            }],
        }

    # -- helpers ---------------------------------------------------------------

    async def _open_cart(self, caller, conversation_id, *, create: bool):
        if caller is None:
            raise CartError("REQUIERE_SESION", "cart needs a customer or a conversation")
        # A conversation id is only trusted after proving ownership, for any
        # caller: an authenticated customer must not read or write someone
        # else's conversation memory (ultimoCarrusel), audit rows or touch().
        if conversation_id is not None:
            owned = await self._conversations.find_owned(conversation_id, caller)
            if owned is None:
                raise CartError("RECURSO_NO_ENCONTRADO", "conversation not found")
        if caller.customer_id is not None:
            cart = await self._carts.obtener_abierto_por_cliente(caller.customer_id)
            if cart is None and create:
                cart = await self._carts.crear_para_cliente(caller.customer_id)
            return cart
        if conversation_id is None:
            raise CartError("RECURSO_NO_ENCONTRADO", "anonymous cart needs a conversation")
        cart = await self._carts.obtener_abierto_por_conversacion(conversation_id)
        if cart is not None and self._expired(cart):
            await self._carts.cambiar_estado(cart.id, "ABANDONED")
            cart = None
        if cart is None and create:
            cart = await self._carts.crear_anonimo(conversation_id)
        return cart

    @staticmethod
    def _expired(cart) -> bool:
        updated = getattr(cart, "updated_at", None)
        if updated is None:
            return False
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - updated > ANONYMOUS_TTL

    async def _line_count(self, cart) -> int:
        return len(await self._items.listar_por_cart(cart.id))

    async def _resolve_line(self, cart, item_id, item_ref, name):
        rows = await self._items.listar_por_cart(cart.id)
        refs = [{"item_id": str(r.id), "name": r.name} for r in rows]
        resolution = self._lines.resolve(
            refs, item_id=item_id, item_ref=item_ref, name=name
        )
        if not resolution.found:
            if resolution.ambiguous:
                options = ", ".join(o["name"] for o in resolution.ambiguous)
                raise CartError("LINEA_AMBIGUA", f"¿Cuáles? {options}",
                                {"opciones": list(resolution.ambiguous)})
            raise CartError("LINEA_NO_ENCONTRADA", "that cart line does not exist")
        for row in rows:
            if str(row.id) == resolution.item_id:
                return row
        raise CartError("LINEA_NO_ENCONTRADA", "that cart line does not exist")

    async def _resolve_sku(self, cart, conversation_id, sku, producto_ref,
                           product_id, size, color):
        if sku:
            return await self._sku_context(sku, size, color, product_id)
        memory = await self._memory(conversation_id)
        resolved_id = resolve_product_id(
            memory, producto_ref=producto_ref, product_id=product_id
        )
        if resolved_id is None:
            return None
        try:
            detail = await self._catalog.get_product_detail(resolved_id)
        except Exception as exc:
            raise self._map_module_error(exc, inactive=True) from exc
        match = self._variants.resolve(detail, size=size, color=color)
        if match.sku is not None:
            return match.sku, detail, {"sku": match.sku, "talla": size, "color": color}
        if match.missing:
            return None, detail, {"faltantes": list(match.missing)}
        return None, detail, None

    async def _sku_context(self, sku, size, color, product_id=None):
        """Hydrate an explicit product, or verify exact SKU matches from bounded search.

        If G6 cannot resolve a bare SKU, retain the line without merchandising.
        """
        detail: dict[str, Any] | None = None
        if product_id:
            try:
                detail = await self._catalog.get_product_detail(product_id)
            except Exception as exc:
                raise self._map_module_error(exc, inactive=True) from exc
            variants = normalize_detail(detail)["variants"]
            match = next((row for row in variants if row.get("sku") == sku), None)
            if str(detail.get("product_id")) != product_id or (
                match is None and (variants or detail.get("sku_base") != sku)
            ):
                raise CartError("PRODUCTO_NO_DISPONIBLE", "SKU does not belong to the supplied product")
            for label, selected in (("talla", size), ("color", color)):
                actual = (match or {}).get(label)
                if selected is not None and (
                    not isinstance(actual, str) or selected.casefold() != actual.casefold()
                ):
                    raise CartError("PRODUCTO_NO_DISPONIBLE", "SKU attributes do not match the selection")
        else:
            try:
                page = await self._catalog.search_products(q=sku, size=10)
                for candidate in page.get("items", []):
                    try:
                        found = await self._catalog.get_product_detail(candidate["product_id"])
                    except ModuleRequestError as exc:
                        if exc.status == 404:
                            continue
                        raise
                    if any(v.get("sku") == sku for v in found.get("variants", [])):
                        detail = found
                        break
            except Exception:
                logger.warning("product lookup unavailable for explicit SKU")
        return sku, (detail or {"name": sku, "images": [], "product_id": ""}), {
            "sku": sku, "talla": size, "color": color,
        }

    async def _memory(self, conversation_id) -> dict:
        if conversation_id is None:
            return {}
        return await self._conversations.get_context(conversation_id)

    async def _check_can_sell(self, sku: str, total: int) -> None:
        rows = await self._inventory.get_availability([sku])
        projection = self._stock.check_row(rows[0] if rows else None)
        if not projection.ok:
            raise CartError(projection.code, f"sku {sku} is not sellable")
        verdicts = await self._inventory.verify_quantities(
            [{"sku": sku, "quantity": total}]
        )
        quantity = self._stock.check_quantity(
            (verdicts[0].get("veredicto") if verdicts else None)
        )
        if not quantity.ok:
            raise CartError(quantity.code, f"sku {sku} lacks stock for {total}")

    async def _live_price(self, sku: str, captured=None) -> Decimal:
        # SPEC-11 Req. 3: the subtotal is the *regular* price x quantity;
        # promotions add the discount through the single evaluation.
        try:
            row = await self._catalog.get_price(sku)
        except Exception as exc:
            raise self._map_module_error(exc, inactive=True) from exc
        try:
            price_projection(row)  # Validate currency/validity even though subtotal uses regular.
            regular = amount(row.get("precio_regular"))
        except ValueError as exc:
            raise self._map_module_error(exc) from exc
        if regular is None:
            raise CartError("PRODUCTO_NO_DISPONIBLE", "Precio no disponible")
        if captured is not None:
            captured.append(row)
        return regular

    async def _prices_map(self, skus: list[str], captured=None) -> dict[str, Decimal]:
        try:
            rows = await self._catalog.get_prices(skus)
        except Exception as exc:
            raise self._map_module_error(exc) from exc
        try:
            for row in rows:
                price_projection(row)
                if captured is not None:
                    captured[str(row["sku"])] = row
            return {str(row["sku"]): amount(row["precio_regular"])
                    for row in rows if row.get("precio_regular") is not None}
        except ValueError as exc:
            raise self._map_module_error(exc) from exc

    async def _evaluate(
        self, cart, rows, *, coupon_code=_COUPON_SIN_CAMBIAR
    ) -> tuple[dict | None, bool]:
        codigo = cart.coupon_code if coupon_code is _COUPON_SIN_CAMBIAR else coupon_code
        try:
            result = await self._catalog.evaluate_promotions(
                [{"sku": row.sku, "quantity": row.quantity} for row in rows],
                coupon_code=codigo,
                customer_ref=str(cart.customer_id) if cart.customer_id else None,
            )
            if result.get("currency") != "PEN":
                raise ValueError("unsupported evaluation currency")
            values = {key: amount(result.get(key)) for key in (
                "original_amount", "discount_amount", "result_amount")}
            if any(value is None for value in values.values()):
                raise ValueError("incomplete evaluation totals")
            if values["original_amount"] - values["discount_amount"] != values["result_amount"]:
                raise ValueError("inconsistent evaluation totals")
            return result, True
        except Exception:
            return None, False

    async def _promotion_names(self) -> dict[str, str]:
        try:
            rows = await self._catalog.list_promotions()
        except Exception:
            return {}
        return {
            str(row.get("promotion_id")): str(row.get("name") or row.get("nombre") or "Descuento")
            for row in rows if row.get("promotion_id")
        }

    async def _audit(self, conversation_id, tool: str, code: str, texto: str) -> None:
        await self._messages.add(
            conversation_id, MessageRole.TOOL, texto,
            tool_name=tool, result_code=code,
        )

    async def _touch(self, conversation_id) -> None:
        try:
            await self._conversations.touch(conversation_id)
        except Exception:
            logger.warning("could not touch conversation %s", conversation_id)

    @staticmethod
    def _variant_label(variant: dict | None) -> str | None:
        if not variant:
            return None
        parts = [p for p in (variant.get("talla"), variant.get("color")) if p]
        return " / ".join(str(p) for p in parts) or None

    @staticmethod
    def _variant_suffix(variant: dict | None) -> str:
        label = ManageCartUseCase._variant_label(variant)
        return f" ({label})" if label else ""

    @staticmethod
    def _map_module_error(exc: Exception, *, inactive: bool = False):
        if isinstance(exc, CartError):
            return exc
        if isinstance(exc, ModuleRequestError) and exc.status == 404 and inactive:
            return CartError("PRODUCTO_NO_DISPONIBLE", exc.code)
        if isinstance(exc, ModuleRequestError):
            return CartError(exc.code, exc.detail)
        if isinstance(exc, ModuleUnavailableError):
            return CartError("SERVICIO_NO_DISPONIBLE", exc.module)
        return CartError("SERVICIO_NO_DISPONIBLE", type(exc).__name__)
