"""`mod-orders` wiring: order reads, tracking and the confirmation e-mail.

Orders owns the `order_confirmation` capability that checkout and the shared
outbox worker consume, and reads Despacho through the `dispatch_port`
capability registered by `mod-shipping`.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_checkout.public import (
    provide_checkout_snapshot,
    provide_confirmation_outbox,
    provide_sales_port,
)
from src.modules.mod_orders import MODULE_ID
from src.modules.mod_orders.adapters.fake_email_sender import FakeEmailSender
from src.modules.mod_orders.adapters.http.orders_router import create_orders_router
from src.modules.mod_orders.adapters.internal_email_notifier import InternalEmailNotifier
from src.modules.mod_orders.adapters.jinja_email_renderer import JinjaEmailRenderer
from src.modules.mod_orders.adapters.notification_postgres_adapter import (
    NotificationPostgresAdapter,
)
from src.modules.mod_orders.adapters.smtp_email_adapter import SmtpEmailSender
from src.modules.mod_orders.application.notification_service import NotificationService
from src.modules.mod_orders.application.orders_service import OrdersService
from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.di.capabilities import DISPATCH_PORT, FEEDBACK_NOTIFIER, ORDER_CONFIRMATION
from src.shared.infrastructure.di.registry import registry
from src.shared.infrastructure.http.router_options import RouterOptions
from src.shared.infrastructure.persistence.order_ref_postgres_adapter import OrderRefPostgresAdapter
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

# Without SMTP_HOST the confirmations land here (process-wide, inspectable in local runs).
_FAKE_EMAIL_SENDER = FakeEmailSender()
_EMAIL_RENDERER = JinjaEmailRenderer()

# Tracking needs the Despacho port owned by `mod-shipping`.
REQUIRED_CAPABILITIES = (DISPATCH_PORT,)


def provide_email_sender(settings: Settings | None = None):
    """Explicit `EMAIL_MODE`: SMTP for real delivery, in-memory fake for development (SPEC-16).

    Settings already fails closed in production when the fake would be inferred from
    a missing SMTP_HOST, so reaching the fake here is always an explicit decision.
    """
    settings = settings or get_settings()
    if settings.email_mode == "fake":
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


def provide_feedback_notifier(settings: Settings | None = None) -> InternalEmailNotifier:
    """Internal e-mail for SPEC-24, reusing the same EmailSender as SPEC-16."""
    settings = settings or get_settings()
    return InternalEmailNotifier(provide_email_sender(settings), settings.feedback_inbox_email)


def build_notification_service(session: AsyncSession) -> NotificationService:
    """Order confirmation e-mail (SPEC-16). The outbox worker builds one per
    SEND_EMAIL batch and calls `handle_send_email_task`; checkout calls
    `enqueue_confirmation(order_id)` once the order is PAID_NOTIFIED."""
    settings = get_settings()
    return NotificationService(
        notifications=NotificationPostgresAdapter(session),
        outbox=provide_confirmation_outbox(session),
        sources=provide_checkout_snapshot(session),
        sender=provide_email_sender(settings),
        templates=_EMAIL_RENDERER,
        uow=SqlAlchemyUnitOfWork(session),
        frontend_base_url=settings.frontend_base_url,
        send_inline=settings.notification_send_inline,
    )


def provide_orders_service(session: AsyncSession = Depends(get_session)) -> OrdersService:
    settings = get_settings()
    dispatch = registry.resolve(DISPATCH_PORT, settings)
    return OrdersService(
        notifications=build_notification_service(session),
        sales_client=provide_sales_port(settings),
        dispatch=dispatch,
        order_refs=OrderRefPostgresAdapter(session),
    )


def build_router(options: RouterOptions) -> APIRouter:
    return create_orders_router(provide_orders_service)


router = build_router(RouterOptions())


def configure(container) -> None:
    """The confirmation service is consumed by checkout and the outbox worker."""
    container.register(ORDER_CONFIRMATION, build_notification_service)
    container.register(FEEDBACK_NOTIFIER, provide_feedback_notifier)


__all__ = [
    "MODULE_ID",
    "ORDER_CONFIRMATION",
    "OrdersService",
    "REQUIRED_CAPABILITIES",
    "build_notification_service",
    "build_router",
    "configure",
    "provide_email_sender",
    "provide_feedback_notifier",
    "provide_orders_service",
    "router",
]
