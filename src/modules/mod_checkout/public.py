"""Public surface of `mod-checkout`.

`mod-orders` (which depends on checkout) reuses the Ventas port wiring and the
checkout document read model; nothing else crosses the boundary.
"""
from src.modules.mod_checkout.application.manage_checkout_use_case import (
    CheckoutError,
    ManageCheckoutUseCase,
)
from src.modules.mod_checkout.module import (
    ORDER_CONFIRMATION,
    OUTBOX_PROCESSOR,
    provide_checkout_service,
    provide_checkout_snapshot,
    provide_confirmation_outbox,
    provide_outbox_processor,
    provide_sales_port,
)
from src.modules.mod_checkout.ports.payment_gateway_port import PaymentGatewayPort

__all__ = [
    "CheckoutError",
    "ManageCheckoutUseCase",
    "ORDER_CONFIRMATION",
    "OUTBOX_PROCESSOR",
    "PaymentGatewayPort",
    "provide_checkout_service",
    "provide_checkout_snapshot",
    "provide_confirmation_outbox",
    "provide_outbox_processor",
    "provide_sales_port",
]
