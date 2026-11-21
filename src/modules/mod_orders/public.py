"""Public surface of `mod-orders`.

`mod-chat` builds the order tools and the direct-action dispatcher from here.
The rest of the module (persistence adapters, e-mail rendering) is private.
"""
from src.modules.mod_checkout.public import provide_sales_port
from src.modules.mod_orders.application.notification_service import (
    NotificationError,
    NotificationService,
)
from src.modules.mod_orders.application.orders_service import OrdersService
from src.modules.mod_orders.domain.status_mapper import (
    SALES_STATUS_LABELS,
    map_order_status_label,
)
from src.modules.mod_orders.domain.tracking_mapper import sanitize_tracking_payload
from src.modules.mod_orders.module import (
    ORDER_CONFIRMATION,
    build_notification_service,
    provide_orders_service,
)

__all__ = [
    "NotificationError",
    "NotificationService",
    "ORDER_CONFIRMATION",
    "OrdersService",
    "SALES_STATUS_LABELS",
    "build_notification_service",
    "map_order_status_label",
    "provide_orders_service",
    "provide_sales_port",
    "sanitize_tracking_payload",
]
