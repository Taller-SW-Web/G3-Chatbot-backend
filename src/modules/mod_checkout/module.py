"""`mod-checkout` wiring: checkout creation, payment and the transactional outbox.

Checkout is also the outbox orchestrator: it registers the processor that the
shared worker lifespan resolves, and the `checkout_document` capability that
`mod-auth` reads for `ultimoDocumento`.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_cart.public import provide_cart_repositories, provide_cart_service
from src.modules.mod_checkout import MODULE_ID
from src.modules.mod_checkout.adapters.checkout_postgres_adapter import CheckoutPostgresAdapter
from src.modules.mod_checkout.adapters.checkout_snapshot_adapter import (
    CheckoutSnapshotPostgresAdapter,
)
from src.modules.mod_checkout.adapters.http.checkout_router import create_checkout_router
from src.modules.mod_checkout.adapters.http.purchase_rating_router import (
    create_purchase_rating_router,
)
from src.modules.mod_checkout.adapters.payment_attempt_postgres_adapter import (
    PaymentAttemptPostgresAdapter,
)
from src.modules.mod_checkout.adapters.payment_simulator_adapter import PaymentSimulatorAdapter
from src.modules.mod_checkout.adapters.purchase_rating_postgres_adapter import (
    PurchaseRatingPostgresAdapter,
)
from src.modules.mod_checkout.application.manage_checkout_use_case import ManageCheckoutUseCase
from src.modules.mod_checkout.application.purchase_rating_service import PurchaseRatingService
from src.modules.mod_shipping.public import provide_despacho_client
from src.shared.application.ports.order_repository_port import SalesOrderPort
from src.shared.infrastructure.auth.providers import (
    provide_security_port,
    provide_service_token_provider,
)
from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.di.capabilities import (
    CHECKOUT_DOCUMENT,
    CHECKOUT_EXPIRY,
    FEEDBACK_NOTIFIER,
    ORDER_CONFIRMATION,
    OUTBOX_PROCESSOR,
    OUTBOX_PUBLISHER,
    OUTBOX_TASK_HANDLER,
)
from src.shared.infrastructure.di.registry import registry
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort
from src.shared.infrastructure.http.router_options import RouterOptions
from src.shared.infrastructure.http.ventas_client import VentasClient
from src.shared.infrastructure.persistence.local_phone_verification_postgres_adapter import (
    LocalPhoneVerificationPostgresAdapter,
)
from src.shared.infrastructure.persistence.order_ref_postgres_adapter import (
    OrderRefPostgresAdapter,
)
from src.shared.infrastructure.persistence.outbox_postgres_adapter import OutboxPostgresAdapter
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.shared.infrastructure.worker.outbox_handler import OutboxTaskHandler
from src.shared.infrastructure.worker.outbox_worker import INLINE_EMAIL_GRACE, OutboxProcessor

# Process-wide so orders created by checkout are visible to the orders module
# while `SALES_MODE=fake` (same lifecycle as the previous container singleton).
_FAKE_SALES_PORT = FakeSalesPort()

# Checkout cannot serve payment confirmation without the orders capability.
REQUIRED_CAPABILITIES = (ORDER_CONFIRMATION,)


def provide_sales_port(settings: Settings | None = None) -> SalesOrderPort:
    """Ventas order port (SPEC-15, SPEC-17): fake in-memory or typed HTTP client."""
    settings = settings or get_settings()
    if settings.module_mode("sales") == "fake":
        return _FAKE_SALES_PORT
    return VentasClient(
        base_url=settings.sales_base_url,
        timeout_seconds=settings.sales_timeout_seconds,
        service_token_provider=provide_service_token_provider(settings),
    )


def provide_payment_gateway(settings: Settings | None = None) -> PaymentSimulatorAdapter:
    """Simulator payment gateway; the 5 s latency card follows `PAYMENT_SIMULATOR_LATENCY_ENABLED`."""
    settings = settings or get_settings()
    return PaymentSimulatorAdapter(simulate_latency=settings.payment_simulator_latency_enabled)


def provide_checkout_snapshot(session: AsyncSession) -> CheckoutSnapshotPostgresAdapter:
    """Read-only checkout/order snapshot consumed by `mod-orders` (SPEC-16)."""
    return CheckoutSnapshotPostgresAdapter(session)


def provide_confirmation_outbox(session: AsyncSession) -> OutboxPostgresAdapter:
    """Outbox writes for the confirmation e-mail; checkout owns the transaction boundary."""
    return OutboxPostgresAdapter(session)


def _notification_service(session: AsyncSession):
    """Order confirmation e-mail owned by `mod-orders`; validated at startup (H-08)."""
    return registry.resolve(ORDER_CONFIRMATION, session)


def provide_checkout_service(
    session: AsyncSession = Depends(get_session),
) -> ManageCheckoutUseCase:
    return _build_checkout_service(session)


def provide_checkout_expirer(session: AsyncSession) -> ManageCheckoutUseCase:
    """Expiry sweeper (`ExpirarCheckouts`): the same use case, driven by the worker loop."""
    return _build_checkout_service(session)


def _build_checkout_service(session: AsyncSession) -> ManageCheckoutUseCase:
    settings = get_settings()
    cart_repositories = provide_cart_repositories(session)
    cart_service = provide_cart_service(session)
    return ManageCheckoutUseCase(
        checkouts=CheckoutPostgresAdapter(session),
        payment_attempts=PaymentAttemptPostgresAdapter(session),
        payment_gateway=provide_payment_gateway(settings),
        uow=SqlAlchemyUnitOfWork(session),
        security_client=provide_security_port(settings),
        carts=cart_repositories.carts,
        cart_items=cart_repositories.items,
        orders=OrderRefPostgresAdapter(session),
        phone_verifications=LocalPhoneVerificationPostgresAdapter(session),
        sales_client=provide_sales_port(settings),
        sales_deadline_seconds=settings.checkout_sales_deadline_seconds,
        outbox=OutboxPostgresAdapter(session),
        dispatch=provide_despacho_client(settings),
        require_phone_verification=settings.require_phone_verification,
        require_verified_contact=settings.require_verified_contact,
        cart_validator=cart_service.validate_cart,
        cart_totals=cart_service.recalculate_totals,
        save_address_enabled=settings.checkout_save_address_enabled,
    )


def provide_outbox_processor(session: AsyncSession) -> OutboxProcessor:
    settings = get_settings()
    return OutboxProcessor(
        outbox_repo=OutboxPostgresAdapter(session),
        order_ref_repo=OrderRefPostgresAdapter(session),
        sales_client=provide_sales_port(settings),
        notification_service=_notification_service(session),
        uow=SqlAlchemyUnitOfWork(session),
        # With inline sending on, fresh SEND_EMAIL jobs are left to the request that created them.
        email_grace=INLINE_EMAIL_GRACE if settings.notification_send_inline else None,
        feedback_notifier=(
            registry.resolve(FEEDBACK_NOTIFIER) if registry.is_registered(FEEDBACK_NOTIFIER) else None
        ),
        # Broker mode relays to RabbitMQ; the lifespan owns the connection (ADR-0030).
        publisher=registry.resolve(OUTBOX_PUBLISHER) if settings.outbox_dispatch_mode == "broker" else None,
    )


def provide_outbox_task_handler(session: AsyncSession) -> OutboxTaskHandler:
    """Per-type dispatch for the broker consumer (ADR-0030): one handler per message and session."""
    return OutboxTaskHandler(
        outbox_repo=OutboxPostgresAdapter(session),
        order_ref_repo=OrderRefPostgresAdapter(session),
        sales_client=provide_sales_port(get_settings()),
        notification_service=_notification_service(session),
        feedback_notifier=(
            registry.resolve(FEEDBACK_NOTIFIER) if registry.is_registered(FEEDBACK_NOTIFIER) else None
        ),
    )


def provide_purchase_rating_service(
    session: AsyncSession = Depends(get_session),
) -> PurchaseRatingService:
    settings = get_settings()
    return PurchaseRatingService(
        ratings=PurchaseRatingPostgresAdapter(session),
        sales=provide_sales_port(settings),
        uow=SqlAlchemyUnitOfWork(session),
    )


def build_router(options: RouterOptions) -> APIRouter:
    router = APIRouter()
    router.include_router(create_checkout_router(provide_checkout_service))
    router.include_router(create_purchase_rating_router(provide_purchase_rating_service))
    return router


router = build_router(RouterOptions())


def configure(container) -> None:
    """Checkout exposes the checkout document and the outbox processor."""
    container.register(CHECKOUT_DOCUMENT, CheckoutPostgresAdapter)
    container.register(OUTBOX_PROCESSOR, provide_outbox_processor)
    container.register(OUTBOX_TASK_HANDLER, provide_outbox_task_handler)
    container.register(CHECKOUT_EXPIRY, provide_checkout_expirer)


__all__ = [
    "CHECKOUT_DOCUMENT",
    "CHECKOUT_EXPIRY",
    "MODULE_ID",
    "ORDER_CONFIRMATION",
    "OUTBOX_PROCESSOR",
    "OUTBOX_TASK_HANDLER",
    "REQUIRED_CAPABILITIES",
    "build_router",
    "configure",
    "provide_checkout_expirer",
    "provide_checkout_service",
    "provide_checkout_snapshot",
    "provide_confirmation_outbox",
    "provide_outbox_processor",
    "provide_outbox_task_handler",
    "provide_payment_gateway",
    "provide_purchase_rating_service",
    "provide_sales_port",
    "router",
]
