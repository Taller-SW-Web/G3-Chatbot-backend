"""`mod-orders` wiring: order reads, tracking and the confirmation e-mail.

Orders owns the `order_confirmation` capability that checkout and the shared
outbox worker consume, and reads Despacho through the `dispatch_port`
capability registered by `mod-shipping`.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_checkout.public import provide_sales_port
from src.modules.mod_orders import MODULE_ID
from src.modules.mod_orders.adapters.fake_email_sender import FakeEmailSender
from src.modules.mod_orders.adapters.http.orders_router import create_orders_router
from src.modules.mod_orders.adapters.jinja_email_renderer import JinjaEmailRenderer
from src.modules.mod_orders.adapters.notification_postgres_adapter import (
    NotificationPostgresAdapter,
)
from src.modules.mod_orders.adapters.order_confirmation_source import (
    OrderConfirmationPostgresAdapter,
)
from src.modules.mod_orders.adapters.smtp_email_adapter import SmtpEmailSender
from src.modules.mod_orders.application.notification_service import NotificationService
from src.modules.mod_orders.application.orders_service import OrdersService
from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.di.capabilities import DISPATCH_PORT, ORDER_CONFIRMATION
from src.shared.infrastructure.di.registry import registry
from src.shared.infrastructure.http.router_options import RouterOptions
from src.shared.infrastructure.persistence.outbox_postgres_adapter import OutboxPostgresAdapter
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

# Without SMTP_HOST the confirmations land here (process-wide, inspectable in local runs).
_FAKE_EMAIL_SENDER = FakeEmailSender()
_EMAIL_RENDERER = JinjaEmailRenderer()


def provide_email_sender(settings: Settings | None = None):
    """SMTP when SMTP_HOST is set (Mailtrap in development); otherwise the in-memory fake (SPEC-16)."""
    settings = settings or get_settings()
    if not settings.smtp_host:
        return _FAKE_EMAIL_SENDER
    return SmtpEmailSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        sender=settings.smtp_sender,
        username=settings.smtp_username,
        password=settings.smtp_password,
        starttls=settings.smtp_starttls,
        timeout_seconds=settings.smtp_timeout_seconds,
    )


def build_notification_service(session: AsyncSession) -> NotificationService:
    """Order confirmation e-mail (SPEC-16). The outbox worker builds one per
    SEND_EMAIL batch and calls `handle_send_email_task`; checkout calls
    `enqueue_confirmation(order_id)` once the order is PAID_NOTIFIED."""
    settings = get_settings()
    return NotificationService(
        notifications=NotificationPostgresAdapter(session),
        outbox=OutboxPostgresAdapter(session),
        sources=OrderConfirmationPostgresAdapter(session),
        sender=provide_email_sender(settings),
        templates=_EMAIL_RENDERER,
        uow=SqlAlchemyUnitOfWork(session),
        frontend_base_url=settings.frontend_base_url,
        send_inline=settings.notification_send_inline,
    )


def provide_orders_service(session: AsyncSession = Depends(get_session)) -> OrdersService:
    settings = get_settings()
    dispatch = (
        registry.resolve(DISPATCH_PORT, settings) if registry.is_registered(DISPATCH_PORT) else None
    )
    return OrdersService(
        notifications=build_notification_service(session),
        sales_client=provide_sales_port(settings),
        dispatch=dispatch,
    )


def build_router(options: RouterOptions) -> APIRouter:
    return create_orders_router(provide_orders_service)


router = build_router(RouterOptions())


def configure(container) -> None:
    """The confirmation service is consumed by checkout and the outbox worker."""
    container.register(ORDER_CONFIRMATION, build_notification_service)


__all__ = [
    "MODULE_ID",
    "ORDER_CONFIRMATION",
    "OrdersService",
    "build_notification_service",
    "build_router",
    "configure",
    "provide_email_sender",
    "provide_orders_service",
    "router",
]
