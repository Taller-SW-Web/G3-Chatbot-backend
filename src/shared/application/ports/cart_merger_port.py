"""Outbound port: merge an anonymous session cart into a customer cart on login (SPEC-03 Req. 4).

`mod-auth` sits at the root of the dependency DAG (`dependsOn: []`), so it cannot
import `mod-cart` directly. `mod-cart` registers an implementation under the
`CART_MERGER` capability and `mod-auth` resolves it at request time.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

CART_MERGED_MESSAGE = "Unimos los productos que agregaste con los de tu cuenta"


@dataclass(frozen=True)
class CartMergeResult:
    merged: bool
    message: str | None = None
    adjustments: list[dict[str, Any]] = field(default_factory=list)


class CartMergerPort(Protocol):
    async def merge_anonymous_cart(
        self,
        *,
        anonymous_sid: str,
        customer_id: uuid.UUID,
    ) -> CartMergeResult:
        """Merge open anonymous carts for `anonymous_sid` into `customer_id`'s cart."""
        ...
