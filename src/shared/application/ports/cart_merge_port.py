"""Cross-module port: merge anonymous cart into customer cart on login (SPEC-03 Req. 4, #184).

Registered by `mod-cart` under the `CART_MERGE` capability and consumed optionally
by `mod-auth` without violating the module DAG (ADR-0024).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class CartMergeResult:
    """Outcome of merging anonymous cart(s) into the authenticated customer's cart."""

    merged: bool = False
    message: str | None = None
    adjustments: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class CartMergePort(Protocol):
    async def merge_for_login(
        self, anonymous_sid: str, customer_id: uuid.UUID
    ) -> CartMergeResult:
        """Merge open anonymous cart(s) linked to hashed `anonymous_sid` into `customer_id`'s cart.

        Does not commit; the caller's unit of work owns the transaction (or the
        implementation commits when invoked standalone).
        """
        ...
