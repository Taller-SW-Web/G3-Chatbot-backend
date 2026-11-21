"""Public surface of `mod-cart`.

`mod-checkout` (which depends on cart) uses the repositories and the use case;
`mod-shipping` reads the same repositories through the `cart_repositories`
capability because the manifest does not let it import this package.
"""
from src.modules.mod_cart.application.manage_cart_use_case import CartError, ManageCartUseCase
from src.modules.mod_cart.module import (
    CART_REPOSITORIES,
    CartRepositories,
    provide_cart_repositories,
    provide_cart_service,
)

__all__ = [
    "CART_REPOSITORIES",
    "CartError",
    "CartRepositories",
    "ManageCartUseCase",
    "provide_cart_repositories",
    "provide_cart_service",
]
