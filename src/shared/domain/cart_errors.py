"""Cart persistence errors shared by the port and the unit of work.

They live in the kernel because `SqlAlchemyUnitOfWork` (shared) translates
driver errors into them and `mod-cart` raises them from its repositories.
"""
from typing import Any


class StaleCartError(Exception):
    """Optimistic-lock conflict: the row was updated by someone else."""

    def __init__(self, detail: Any = ""):
        super().__init__(str(detail))


class CartConflictError(Exception):
    """Unique-key clash, e.g. a first concurrent add opens two carts."""

    def __init__(self, detail: Any = ""):
        super().__init__(str(detail))
