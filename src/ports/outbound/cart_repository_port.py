"""Outbound ports for cart persistence (SPEC-11, ADR-0002).

The cart use case depends on these contracts, never on SQLAlchemy: the
adapters build the rows and translate driver errors into the port errors
below. That keeps `manage_cart_use_case` free of infrastructure imports.

- `StaleCartError`: the cart row changed in another transaction (optimistic
  `version`). The use case retries the mutation once.
- `CartConflictError`: two concurrent writes collided on a unique key (one
  open cart per customer/conversation). Mapped to `CONFLICTO`.
"""
from typing import Any, Protocol


class StaleCartError(Exception):
    """Optimistic-lock conflict: the row was updated by someone else."""


class CartConflictError(Exception):
    """Unique-key clash, e.g. a first concurrent add opens two carts."""


class CartRepository(Protocol):
    async def obtener_abierto_por_cliente(self, customer_id) -> Any: ...

    async def obtener_abierto_por_conversacion(self, conversation_id) -> Any: ...

    async def crear_para_cliente(self, customer_id) -> Any: ...

    async def crear_anonimo(self, conversation_id) -> Any: ...

    async def cambiar_estado(self, cart_id, estado: str) -> Any: ...

    async def set_totals(
        self, cart_id, snapshot: dict[str, Any] | None
    ) -> None: ...

    async def reset_checkout(self, cart_id) -> None:
        """Drop the coupon/shipping/totals snapshots (empty the cart)."""
        ...

    async def touch(self, cart_id) -> None:
        """Bump `updated_at` so the optimistic `version` advances on read."""
        ...


class CartItemRepository(Protocol):
    async def listar_por_cart(self, cart_id) -> list[Any]: ...

    async def obtener_por_cart_y_sku(self, cart_id, sku: str) -> Any: ...

    async def agregar(
        self,
        *,
        cart_id,
        product_id: str,
        sku: str,
        name: str,
        variant_description: str | None,
        image_url: str | None,
        quantity: int,
        unit_price_ref: float,
    ) -> Any: ...

    async def set_quantity(
        self, item_id, *, quantity: int, unit_price_ref: float
    ) -> None: ...

    async def eliminar(self, item_id) -> bool: ...
