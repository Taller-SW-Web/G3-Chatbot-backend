"""Cart use case: add, change, remove, clear and view (SPEC-11, SPEC-10).

Owns cart lifecycle (anonymous per conversation with 7-day expiry,
single ACTIVE cart per customer), live stock checks, live totals and
TOOL audit rows. Applying coupons (SPEC-13) and quoting shipping
(SPEC-12) are out of scope: the stored coupon is only revalidated
through the promotions evaluation, and shipping stays empty until
checkout quotes it. Never imports FastAPI, httpx or SQLAlchemy.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm.exc import StaleDataError

from src.adapters.outbound.persistence.models.cart import Cart
from src.adapters.outbound.persistence.models.cart_item import CartItem
from src.application.catalog.product_lookup import (
    read_memory,
    resolve_product_id,
)
from src.domain.entities.message import MessageRole
from src.domain.services.cart_line_resolver import CartLineResolver
from src.domain.services.stock_validator import StockValidator
from src.domain.services.totals_calculator import PricedLine, TotalsCalculator
from src.domain.services.variant_resolver import VariantResolver

MAX_PER_LINE = 10
MAX_LINES = 20
ANONYMOUS_TTL = timedelta(days=7)


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
    ):
        self._carts = carts
        self._items = items
        self._catalog = catalog
        self._inventory = inventory
        self._conversations = conversations
        self._messages = messages
        self._uow = uow
        self._totals = totals or TotalsCalculator()
        self._lines = CartLineResolver()
        self._variants = VariantResolver()
        self._stock = StockValidator()

    # -- public operations ----------------------------------------------------

    async def add(self, caller, conversation_id: uuid.UUID, *, sku=None,
                  producto_ref=None, product_id=None, size=None, color=None,
                  quantity: int = 1, tool: str = "agregar_al_carrito") -> dict:
        if not 1 <= quantity <= MAX_PER_LINE:
            raise CartError("LIMITE_CANTIDAD", f"quantity {quantity} out of 1..10")
        return await self._mutate(
            caller, conversation_id, tool,
            lambda cart: self._add_line(cart, conversation_id, sku, producto_ref,
                                        product_id, size, color, quantity),
        )

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
            cart.coupon_code = None
            cart.shipping_snapshot = None
            cart.totals_snapshot = None
            return "Vaciamos tu carrito."
        return await self._mutate(caller, conversation_id, tool, _clear)

    async def view(self, caller, conversation_id: uuid.UUID) -> dict:
        cart = await self._open_cart(caller, conversation_id, create=False)
        if cart is None:
            return {"code": "OK", "texto": "Tu carrito está vacío", "bloques": []}
        return await self._cart_view(cart, conversation_id)

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
                view["texto"] = texto
                return view
            except StaleDataError:
                await self._uow.rollback()
                if attempts >= 2:
                    raise CartError("CONFLICTO", "concurrent modification, retry")
            except CartError as exc:
                await self._uow.rollback()
                if exc.code == "VARIANTE_REQUERIDA":
                    return {
                        "code": "OK",
                        "texto": exc.detail,
                        "bloques": [{
                            "id": uuid.uuid4().hex,
                            "tipo": "SELECTOR_VARIANTE",
                            "datos": exc.data,
                        }],
                    }
                raise
            except Exception as exc:
                await self._uow.rollback()
                raise self._map_module_error(exc) from exc

    async def _add_line(self, cart, conversation_id, sku, producto_ref,
                        product_id, size, color, quantity) -> str:
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
        price = await self._live_price(sku_value)
        if existing is None:
            await self._items.guardar(CartItem(
                cart_id=cart.id,
                product_id=str(detail.get("product_id", "")),
                sku=sku_value,
                name=str(detail.get("name", sku_value)),
                variant_description=self._variant_label(variant),
                image_url=(detail.get("images") or [None])[0],
                quantity=quantity,
                unit_price_ref=float(price),
            ))
        else:
            existing.quantity = current + quantity
            existing.unit_price_ref = float(price)
        return f"Agregué {detail.get('name', sku_value)}.{self._variant_suffix(variant)}"

    async def _set_quantity(self, cart, item_id, item_ref, name, quantity) -> str:
        row = await self._resolve_line(cart, item_id, item_ref, name)
        if quantity == 0:
            await self._items.eliminar(row.id)
            return f"Quité {row.name} del carrito."
        if not 1 <= quantity <= MAX_PER_LINE:
            raise CartError("LIMITE_CANTIDAD", "El máximo por producto es 10 unidades")
        await self._check_can_sell(row.sku, quantity)
        row.quantity = quantity
        row.unit_price_ref = float(await self._live_price(row.sku))
        return f"Cambié {row.name} a {quantity}."

    async def _drop_line(self, cart, item_id, item_ref, name) -> str:
        row = await self._resolve_line(cart, item_id, item_ref, name)
        await self._items.eliminar(row.id)
        return f"Quité {row.name} del carrito."

    # -- reads ----------------------------------------------------------------

    async def _cart_view(self, cart, conversation_id, *, touch=True) -> dict:
        rows = await self._items.listar_por_cart(cart.id)
        if not rows:
            return {"code": "OK", "texto": "Tu carrito está vacío", "bloques": []}
        skus = [row.sku for row in rows]
        prices = await self._prices_map(skus)
        evaluation, promotions_ok = await self._evaluate(cart, rows)
        names = await self._promotion_names()
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
        cart.totals_snapshot = {
            "subtotal": str(totals.subtotal),
            "descuentos": [
                {"name": d["name"], "amount": str(d["amount"])} for d in totals.discounts
            ],
            "total": str(totals.total),
        }
        if touch and conversation_id is not None:
            await self._touch(conversation_id)
        lines_out = [
            {
                "item_id": str(row.id),
                "nombre": row.name,
                "variante": row.variant_description,
                "cantidad": row.quantity,
                "precio": float(priced[i].unit_price),
                "disponible": priced[i].available,
                "imagen": row.image_url,
            }
            for i, row in enumerate(rows)
        ]
        unavailable = [line for i, line in enumerate(lines_out) if not priced[i].available]
        texto = f"Tu carrito: {len(rows)} productos · S/ {totals.total:.2f}"
        notices = list(totals.notices)
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
                },
            }],
        }

    # -- helpers ---------------------------------------------------------------

    async def _open_cart(self, caller, conversation_id, *, create: bool):
        if caller is None:
            raise CartError("REQUIERE_SESION", "cart needs a customer or a conversation")
        if caller.customer_id is not None:
            cart = await self._carts.obtener_abierto_por_cliente(caller.customer_id)
            if cart is None and create:
                cart = await self._carts.guardar(Cart(customer_id=caller.customer_id))
            return cart
        if conversation_id is None:
            raise CartError("RECURSO_NO_ENCONTRADO", "anonymous cart needs a conversation")
        owned = await self._conversations.find_owned(conversation_id, caller)
        if owned is None:
            raise CartError("RECURSO_NO_ENCONTRADO", "conversation not found")
        cart = await self._carts.obtener_abierto_por_conversacion(conversation_id)
        if cart is not None and self._expired(cart):
            await self._carts.cambiar_estado(cart.id, "ABANDONED")
            cart = None
        if cart is None and create:
            cart = await self._carts.guardar(Cart(conversation_id=conversation_id))
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
            return await self._sku_context(sku, size, color)
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

    async def _sku_context(self, sku, size, color):
        return sku, {"name": sku, "images": []}, {"sku": sku, "talla": size, "color": color}

    async def _memory(self, conversation_id) -> dict:
        from src.application.catalog.product_lookup import read_memory

        row = await self._conversations.obtener_por_id(conversation_id)
        return read_memory(row) if row else {}

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

    async def _live_price(self, sku: str) -> Decimal:
        try:
            row = await self._catalog.get_price(sku)
        except Exception as exc:
            raise self._map_module_error(exc, inactive=True) from exc
        offer = row.get("precio_oferta")
        return Decimal(str(offer if offer is not None else row.get("precio_regular", 0)))

    async def _prices_map(self, skus: list[str]) -> dict[str, Decimal]:
        try:
            rows = await self._catalog.get_prices(skus)
        except Exception as exc:
            raise self._map_module_error(exc) from exc
        out: dict[str, Decimal] = {}
        for row in rows:
            offer = row.get("precio_oferta")
            out[str(row.get("sku"))] = Decimal(
                str(offer if offer is not None else row.get("precio_regular", 0))
            )
        return out

    async def _evaluate(self, cart, rows) -> tuple[dict | None, bool]:
        try:
            result = await self._catalog.evaluate_promotions(
                [{"sku": row.sku, "quantity": row.quantity} for row in rows],
                coupon_code=cart.coupon_code,
                customer_ref=str(cart.customer_id) if cart.customer_id else None,
            )
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
            pass

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
        from src.ports.outbound.module_errors import (
            ModuleRequestError,
            ModuleUnavailableError,
        )

        if isinstance(exc, CartError):
            return exc
        if isinstance(exc, ModuleRequestError) and exc.status == 404 and inactive:
            return CartError("PRODUCTO_NO_DISPONIBLE", exc.code)
        if isinstance(exc, ModuleRequestError):
            return CartError(exc.code, exc.detail)
        if isinstance(exc, ModuleUnavailableError):
            return CartError("SERVICIO_NO_DISPONIBLE", exc.module)
        return CartError("SERVICIO_NO_DISPONIBLE", type(exc).__name__)
