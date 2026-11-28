"""Merge anonymous cart(s) into the authenticated customer's cart on login (SPEC-03 Req. 4, #184).

Applies:
- Per-line cap (`MAX_QTY_PER_LINE = 10`) and distinct-SKU cap (`MAX_LINES = 20`).
- Live inventory verification via `verify_quantities` (`SUFICIENTE` / `INSUFICIENTE` / `INDETERMINADO`).
  When `INSUFICIENTE`, probes with a bounded binary search (<=4 calls) to clamp to available stock.
- `SIN_STOCK` policy: new anonymous lines with 0 stock are omitted; existing customer lines are
  preserved at their current quantity (never silently deleted) and reported with `motivo: "SIN_STOCK"`
  so `GET /carrito` surfaces them as `Agotado`.
- Marks merged anonymous cart(s) as `MERGED` so fusion runs at most once.
- Invalidates `totals_snapshot` with SQL NULL so the next read recalculates live prices/promotions.
- Ordering invariant: must run BEFORE `link_anonymous_to_customer` (which clears `anonymous_sid`).
  Does not commit; the caller's unit of work owns the transaction.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol

from src.modules.mod_cart.ports.cart_repository_port import CartItemRepository, CartRepository
from src.shared.application.ports.cart_merge_port import CartMergeResult

logger = logging.getLogger(__name__)

MAX_QTY_PER_LINE = 10
MAX_LINES = 20
CART_MERGED_MESSAGE = "Unimos los productos que agregaste con los de tu cuenta"


class _InventoryPort(Protocol):
    async def verify_quantities(
        self,
        lines: list[dict[str, Any]],
        *,
        channel_id: str = "CHATBOT",
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]: ...


class MergeAnonymousCartService:
    def __init__(
        self,
        carts: CartRepository,
        items: CartItemRepository,
        inventory: _InventoryPort | None = None,
    ) -> None:
        self._carts = carts
        self._items = items
        self._inventory = inventory

    async def merge_for_login(
        self, anonymous_sid: str, customer_id: uuid.UUID
    ) -> CartMergeResult:
        if not anonymous_sid:
            return CartMergeResult(merged=False)

        anon_carts = await self._carts.obtener_abiertos_por_anonymous_sid(anonymous_sid)
        if not anon_carts:
            return CartMergeResult(merged=False)

        anon_items_by_cart: list[tuple[Any, list[Any]]] = []
        total_anon_items = 0
        for anon_cart in anon_carts:
            cart_items = await self._items.listar_por_cart(anon_cart.id)
            anon_items_by_cart.append((anon_cart, cart_items))
            total_anon_items += len(cart_items)

        if total_anon_items == 0:
            for anon_cart, _ in anon_items_by_cart:
                await self._carts.cambiar_estado(anon_cart.id, "MERGED")
            return CartMergeResult(merged=False)

        customer_cart = await self._carts.obtener_abierto_por_cliente(customer_id)
        if customer_cart is None:
            customer_cart = await self._carts.crear_para_cliente(customer_id)

        existing_items = await self._items.listar_por_cart(customer_cart.id)
        by_sku: dict[str, Any] = {item.sku: item for item in existing_items}
        adjustments: list[dict[str, Any]] = []
        merged_any = False

        for anon_cart, cart_items in anon_items_by_cart:
            for anon_item in cart_items:
                sku = str(anon_item.sku)
                existing = by_sku.get(sku)
                if existing is not None:
                    raw_sum = int(existing.quantity) + int(anon_item.quantity)
                    desired = min(MAX_QTY_PER_LINE, raw_sum)
                    allowed_qty, stock_reason = await self._clamp_to_stock(sku, desired)
                    if stock_reason is not None:
                        # Existing customer line: never silently delete on SIN_STOCK; preserve existing qty.
                        final_qty = allowed_qty if allowed_qty > 0 else int(existing.quantity)
                        adjustments.append(
                            {
                                "sku": sku,
                                "solicitado": raw_sum,
                                "ajustado": final_qty,
                                "motivo": stock_reason,
                            }
                        )
                    else:
                        final_qty = desired
                        if raw_sum > MAX_QTY_PER_LINE:
                            adjustments.append(
                                {
                                    "sku": sku,
                                    "solicitado": raw_sum,
                                    "ajustado": final_qty,
                                    "motivo": "TOPE_POR_LINEA",
                                }
                            )
                    if final_qty != int(existing.quantity):
                        await self._items.set_quantity(
                            existing.id,
                            quantity=final_qty,
                            unit_price_ref=float(existing.unit_price_ref),
                        )
                        existing.quantity = final_qty
                    merged_any = True
                else:
                    if len(by_sku) >= MAX_LINES:
                        adjustments.append(
                            {
                                "sku": sku,
                                "solicitado": int(anon_item.quantity),
                                "ajustado": 0,
                                "motivo": "LIMITE_LINEAS",
                            }
                        )
                        continue
                    raw_qty = int(anon_item.quantity)
                    desired = min(MAX_QTY_PER_LINE, raw_qty)
                    allowed_qty, stock_reason = await self._clamp_to_stock(sku, desired)
                    if stock_reason is not None:
                        adjustments.append(
                            {
                                "sku": sku,
                                "solicitado": raw_qty,
                                "ajustado": allowed_qty,
                                "motivo": stock_reason,
                            }
                        )
                    elif raw_qty > MAX_QTY_PER_LINE:
                        adjustments.append(
                            {
                                "sku": sku,
                                "solicitado": raw_qty,
                                "ajustado": desired,
                                "motivo": "TOPE_POR_LINEA",
                            }
                        )
                    if allowed_qty <= 0:
                        continue
                    created = await self._items.agregar(
                        cart_id=customer_cart.id,
                        product_id=str(anon_item.product_id),
                        sku=sku,
                        name=str(anon_item.name),
                        variant_description=anon_item.variant_description,
                        image_url=anon_item.image_url,
                        quantity=allowed_qty,
                        unit_price_ref=float(anon_item.unit_price_ref),
                    )
                    by_sku[sku] = created
                    merged_any = True

            await self._carts.cambiar_estado(anon_cart.id, "MERGED")

        await self._carts.set_totals(customer_cart.id, None)
        return CartMergeResult(
            merged=merged_any,
            message=CART_MERGED_MESSAGE if merged_any else None,
            adjustments=tuple(adjustments),
        )

    async def _verify_single(self, sku: str, quantity: int) -> str | None:
        if self._inventory is None:
            return "SUFICIENTE"
        try:
            verdicts = await self._inventory.verify_quantities([{"sku": sku, "quantity": quantity}])
        except Exception as exc:
            logger.warning("cart merge inventory check degraded for sku=%s: %s", sku, type(exc).__name__)
            return None
        match = next((row for row in verdicts if row.get("sku") == sku), None)
        if not match:
            return None
        return str(match.get("result") or "").upper() or None

    async def _clamp_to_stock(self, sku: str, desired: int) -> tuple[int, str | None]:
        verdict = await self._verify_single(sku, desired)
        if verdict is None or verdict in ("SUFICIENTE", "INDETERMINADO"):
            return desired, None
        # INSUFICIENTE: binary search [1 .. desired - 1] (at most 4 calls since desired <= 10).
        lo, hi = 1, desired - 1
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            probe = await self._verify_single(sku, mid)
            if probe is None or probe in ("SUFICIENTE", "INDETERMINADO"):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best > 0:
            return best, "STOCK_INSUFICIENTE"
        return 0, "SIN_STOCK"
