"""Minimal dependency wiring (ADR-0002): builds use cases from a request-scoped DB session."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.fakes.fake_dispatch import FakeDispatchPort
from src.adapters.outbound.fakes.fake_email_sender import FakeEmailSender
from src.adapters.outbound.fakes.fake_postventa import FakePostventaPort
from src.adapters.outbound.http_clients.despacho_api_adapter import DespachoClient
from src.adapters.outbound.http_clients.postventa_client import PostventaClient
from src.adapters.outbound.nlp.openai_nlp_adapter import OpenAILLMProvider
from src.adapters.outbound.notificaciones.jinja_email_renderer import JinjaEmailRenderer
from src.adapters.outbound.notificaciones.smtp_email_adapter import SmtpEmailSender
from src.adapters.outbound.pagos.payment_simulator_adapter import PaymentSimulatorAdapter
from src.adapters.outbound.persistence.cart_item_postgres_adapter import (
    CartItemPostgresAdapter,
)
from src.adapters.outbound.persistence.cart_postgres_adapter import CartPostgresAdapter
from src.adapters.outbound.persistence.checkout_postgres_adapter import CheckoutPostgresAdapter
from src.adapters.outbound.persistence.notification_postgres_adapter import NotificationPostgresAdapter
from src.adapters.outbound.persistence.order_confirmation_postgres_adapter import (
    OrderConfirmationPostgresAdapter,
)
from src.adapters.outbound.persistence.payment_attempt_postgres_adapter import (
    PaymentAttemptPostgresAdapter,
)
from src.adapters.outbound.tools.registry_tool_adapter import RegistryToolAdapter
from src.application.tools.action_dispatcher import ActionDispatcher
from src.application.tools.cart_handlers import build_cart_handlers
from src.application.tools.catalog_handlers import build_catalog_handlers
from src.application.tools.order_handlers import build_order_handlers
from src.application.use_cases.dispatch_direct_action_use_case import DispatchDirectActionUseCase
from src.application.use_cases.interpret_and_respond_use_case import InterpretAndRespondUseCase
from src.application.use_cases.manage_cart_use_case import ManageCartUseCase
from src.application.use_cases.manage_checkout_use_case import ManageCheckoutUseCase
from src.application.use_cases.manage_conversation_use_case import ManageConversationUseCase
from src.application.use_cases.manage_shipping_use_case import ManageShippingUseCase
from src.application.use_cases.notification_service import NotificationError, NotificationService
from src.domain.services.status_mapper import map_order_status_label
from src.modules.mod_catalog.application.tools import build_tool_registry
from src.modules.mod_catalog.public import (
    provide_catalog_service,
    provide_product_catalog,
    provide_product_inventory,
)
from src.ports.outbound.llm_provider_port import LLMProvider
from src.ports.outbound.tool_executor_port import ToolExecutorPort
from src.shared.application.ports.order_repository_port import SalesOrderPort
from src.shared.application.tools.tool_registry import ToolRegistry
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError
from src.shared.domain.sensitive_data_filter import SensitiveDataFilter
from src.shared.infrastructure.auth.providers import (
    provide_security_port,
    provide_service_token_provider,
)
from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.db import connection
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort
from src.shared.infrastructure.http.base_client import BaseModuleClient
from src.shared.infrastructure.http.ventas_client import VentasClient
from src.shared.infrastructure.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.shared.infrastructure.persistence.local_phone_verification_postgres_adapter import (
    LocalPhoneVerificationPostgresAdapter,
)
from src.shared.infrastructure.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.shared.infrastructure.persistence.order_ref_postgres_adapter import (
    OrderRefPostgresAdapter,
)
from src.shared.infrastructure.persistence.outbox_postgres_adapter import OutboxPostgresAdapter
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.shared.infrastructure.worker.outbox_worker import INLINE_EMAIL_GRACE, OutboxProcessor

# Without SMTP_HOST the confirmations land here (process-wide, inspectable in local runs).
_FAKE_EMAIL_SENDER = FakeEmailSender()
_EMAIL_RENDERER = JinjaEmailRenderer()


def provide_chatbot_service(session: AsyncSession = Depends(get_session)) -> ManageConversationUseCase:
    return ManageConversationUseCase(
        conversations=ConversationPostgresAdapter(session),
        messages=MessagePostgresAdapter(session),
        uow=SqlAlchemyUnitOfWork(session),
    )


def provide_llm_provider(settings: Settings | None = None) -> LLMProvider:
    """Picks the LLM adapter by LLM_PROVIDER. A missing API key only fails when the provider is used."""
    settings = settings or get_settings()
    if settings.llm_provider == "openai":
        return OpenAILLMProvider(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
            vision_enabled=settings.llm_vision_enabled,
            reasoning_effort=settings.llm_reasoning_effort,
        )
    if settings.llm_provider == "claude":
        raise NotImplementedError("LLM_PROVIDER=claude is not implemented yet")
    raise ValueError(f"Unknown LLM_PROVIDER {settings.llm_provider!r} (supported: openai)")


def provide_tool_executor(session: AsyncSession) -> ToolExecutorPort:
    """Tool registry with handlers bound to this request's database session."""
    return RegistryToolAdapter(provide_tool_registry(session))


def provide_tool_registry(session: AsyncSession) -> ToolRegistry:
    """Build the canonical registry for both LLM and direct-UI action paths."""
    catalog = provide_catalog_service(session)
    cart = provide_cart_service(session)
    handlers = {}
    handlers.update(build_catalog_handlers(catalog))
    handlers.update(build_cart_handlers(cart))
    handlers.update(build_order_handlers(provide_orders_service(session)))
    return build_tool_registry(handlers)


def provide_action_dispatcher(session: AsyncSession) -> ActionDispatcher:
    return ActionDispatcher(provide_tool_registry(session))


def provide_cart_service(
    session: AsyncSession = Depends(get_session),
) -> ManageCartUseCase:
    settings = get_settings()
    return ManageCartUseCase(
        CartPostgresAdapter(session),
        CartItemPostgresAdapter(session),
        provide_product_catalog(settings),
        provide_product_inventory(settings),
        ConversationPostgresAdapter(session),
        MessagePostgresAdapter(session),
        SqlAlchemyUnitOfWork(session),
        discovery=provide_catalog_service(session),
    )


def provide_shipping_service(
    session: AsyncSession = Depends(get_session),
) -> ManageShippingUseCase:
    """Shipping quote and saved addresses (SPEC-12).

    The cart ports give the lines the client never sends; Seguridad prefill
    addresses and Despacho the real quote with the service token.
    """
    settings = get_settings()
    return ManageShippingUseCase(
        CartPostgresAdapter(session),
        CartItemPostgresAdapter(session),
        provide_despacho_client(settings),
        provide_security_port(settings),
    )


def sanitize_sensitive_text(text: str) -> str:
    """Redact customer secrets before they are persisted or sent to the LLM."""
    return SensitiveDataFilter().redactar(text).texto


def provide_module_client(module: str, settings: Settings | None = None) -> BaseModuleClient:
    """Module client wiring (ADR-0017): base URL + timeout from Settings.

    Switching mock<->real is config-only: point *_BASE_URL at Prism/respx or
    the real module. The typed clients are built on top of this.
    """
    settings = settings or get_settings()
    wiring = {
        "security": (settings.security_base_url, settings.security_timeout_seconds),
        "products": (settings.products_base_url, settings.products_timeout_seconds),
        "sales": (settings.sales_base_url, settings.sales_timeout_seconds),
        "dispatch": (settings.dispatch_base_url, settings.dispatch_timeout_seconds),
    }
    try:
        base_url, timeout_seconds = wiring[module]
    except KeyError:
        raise ValueError(f"unknown module {module!r} (security/products/sales/dispatch)") from None
    client = BaseModuleClient(base_url=base_url, timeout_seconds=timeout_seconds)
    client.module = module
    return client


_FAKE_SALES_PORT = FakeSalesPort()
_FAKE_POSTVENTA_PORT = FakePostventaPort()


def provide_sales_port(settings: Settings | None = None) -> SalesOrderPort:
    """Sales order port (SPEC-15, SPEC-17): fake in-memory or typed HTTP client."""
    settings = settings or get_settings()
    if settings.module_mode("sales") == "fake":
        return _FAKE_SALES_PORT
    return VentasClient(
        base_url=settings.sales_base_url,
        timeout_seconds=settings.sales_timeout_seconds,
        service_token_provider=provide_service_token_provider(settings),
    )


def provide_postventa_port(settings: Settings | None = None):
    """G5 M2 Postventa: in-process fake in `SALES_MODE=fake`, HTTP otherwise."""
    settings = settings or get_settings()
    if settings.module_mode("sales") == "fake":
        return _FAKE_POSTVENTA_PORT
    return PostventaClient(
        base_url=settings.postventa_base_url,
        timeout_seconds=settings.postventa_timeout_seconds,
    )


def provide_postventa_service(session: AsyncSession = Depends(get_session)):
    """Postventa service with local idempotency and customer-token strategy."""
    settings = get_settings()
    from src.adapters.outbound.persistence.claim_ref_postgres_adapter import ClaimRefPostgresAdapter
    from src.adapters.outbound.persistence.evidence_postgres_adapter import EvidencePostgresAdapter
    from src.adapters.outbound.persistence.return_ref_postgres_adapter import ReturnRefPostgresAdapter
    from src.application.use_cases.postventa_service import PostventaService

    fake = settings.module_mode("sales") == "fake"
    return PostventaService(
        postventa=provide_postventa_port(settings),
        sales=provide_sales_port(settings),
        claim_refs=None if fake else ClaimRefPostgresAdapter(session),
        return_refs=None if fake else ReturnRefPostgresAdapter(session),
        evidence=None if fake else EvidencePostgresAdapter(session),
    )


def provide_despacho_client(settings: Settings | None = None):
    """Despacho port honoring `DISPATCH_MODE` (ADR-0022): in-process fake or HTTP.

    The HTTP client authenticates as `modulo-chatbot` with the service token
    provider (ADR-0009); the fake keeps the demo working without G2 credentials.
    """
    settings = settings or get_settings()
    if settings.module_mode("dispatch") == "fake":
        return FakeDispatchPort()
    return DespachoClient(
        base_url=settings.dispatch_base_url,
        timeout_seconds=settings.dispatch_timeout_seconds,
        service_token_provider=provide_service_token_provider(settings),
    )


def provide_payment_gateway() -> PaymentSimulatorAdapter:
    return PaymentSimulatorAdapter()


def provide_checkout_service(
    session: AsyncSession = Depends(get_session),
) -> ManageCheckoutUseCase:
    settings = get_settings()
    return ManageCheckoutUseCase(
        checkouts=CheckoutPostgresAdapter(session),
        payment_attempts=PaymentAttemptPostgresAdapter(session),
        payment_gateway=provide_payment_gateway(),
        uow=SqlAlchemyUnitOfWork(session),
        security_client=provide_security_port(settings),
        carts=CartPostgresAdapter(session),
        cart_items=CartItemPostgresAdapter(session),
        orders=OrderRefPostgresAdapter(session),
        phone_verifications=LocalPhoneVerificationPostgresAdapter(session),
        notification_service=build_notification_service(session),
        sales_client=provide_sales_port(settings),
        outbox=OutboxPostgresAdapter(session),
        dispatch=provide_despacho_client(settings),
    )

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
    """Order confirmation e-mail (SPEC-16). The outbox worker (SPEC-15) builds one per
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


def _with_status_label(payload: dict, key: str = "estadoEtiqueta") -> dict:
    """Adds the customer-facing label Ventas does not send (contratos-integracion §2.5.2)."""
    if payload.get(key):
        return payload
    return {**payload, key: map_order_status_label(str(payload.get("estado") or ""))}


class OrdersService:
    """`OrdersServicePort` for the orders router: confirmation resend (SPEC-16) and
    customer orders listing / detail (SPEC-17) delegated to Ventas."""

    def __init__(
        self,
        notifications: NotificationService,
        sales_client: SalesOrderPort | None = None,
        dispatch=None,
    ):
        self._notifications = notifications
        self._sales = sales_client
        self._dispatch = dispatch

    async def resend_confirmation(self, customer_id, order_id, *, holder_token, idempotency_key):
        return await self._notifications.resend_confirmation(
            customer_id, order_id, idempotency_key=idempotency_key
        )

    async def list_orders(
        self,
        customer_id,
        *,
        holder_token: str,
        estado: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ):
        if self._sales:
            raw = await self._sales.list_customer_orders(
                holder_token,
                estado=estado,
                page=page,
                page_size=page_size,
            )
            return {**raw, "items": [_with_status_label(item) for item in raw.get("items", [])]}
        raise NotificationError("SERVICIO_NO_DISPONIBLE", "La consulta de pedidos aún no está disponible.")

    async def get_order_detail(
        self,
        customer_id,
        order_id: str,
        *,
        holder_token: str,
    ):
        if self._sales:
            raw = await self._sales.get_customer_order(order_id, holder_token)
            # G5 `PedidoDetalle` keeps money inside `pago` and history in
            # `historialEstados`; the channel contract exposes them at the root.
            payment_raw = raw.get("pago")
            payment: dict[str, Any] = payment_raw if isinstance(payment_raw, dict) else {}
            total = raw.get("total", payment.get("total", 0))
            moneda = raw.get("moneda", payment.get("moneda", "PEN"))
            history = raw.get("historial") or raw.get("historialEstados") or []
            return {
                **_with_status_label({**raw, "total": total, "moneda": moneda}),
                "historial": [
                    {**_with_status_label(m, key="etiqueta"), "completado": m.get("completado", True)}
                    for m in history
                ],
            }
        raise NotificationError("SERVICIO_NO_DISPONIBLE", "La consulta de pedidos aún no está disponible.")

    async def get_order_tracking(
        self,
        customer_id,
        order_id: str,
        *,
        holder_token: str,
    ):
        """Order tracking (SPEC-18).

        First proves ownership through Ventas with the customer token, then asks
        Despacho with the service token. Despacho is optional: a 404 while the
        order is still being prepared, a missing scope or a timeout degrade to
        the order state from Ventas with ``detalleDisponible=false``.
        """
        if self._sales is None:
            raise NotificationError(
                "SERVICIO_NO_DISPONIBLE",
                "La consulta de pedidos aún no está disponible.",
            )
        order = await self._sales.get_customer_order(order_id, holder_token)
        estado = str(order.get("estado") or "").strip().upper()
        envio_raw = order.get("envio")
        envio: dict[str, Any] = envio_raw if isinstance(envio_raw, dict) else {}
        fallback: dict[str, Any] = {
            "pedidoId": order_id,
            "estado": estado,
            "estadoEtiqueta": map_order_status_label(estado),
            "fechaProgramada": envio.get("fechaEstimadaEntrega"),
            "distrito": envio.get("distrito"),
            "hitos": [],
            "detalleDisponible": False,
            "mensaje": (
                "Tu pedido está pendiente de pago."
                if estado == "CREADO"
                else "Tu pedido aún se está preparando; te mostraremos el seguimiento "
                "cuando salga del centro de despacho."
                if estado in {"PAGADO", "EN_PREPARACION"}
                else "Tu pedido fue anulado."
                if estado == "ANULADO"
                else "El detalle del envío no está disponible ahora."
            ),
        }
        # Ventas is the source of truth. Despacho only adds detail once the
        # order is actually dispatched (or delivered); a paid order still in
        # preparation must not show a synthetic "on the way" milestone.
        if estado not in {"DESPACHADO", "ENTREGADO"}:
            return fallback
        if self._dispatch is None:
            return fallback
        try:
            raw = await self._dispatch.get_order_tracking(order_id)
        except (ModuleRequestError, ModuleUnavailableError):
            # A 404 is normal before dispatch, and a missing scope or timeout
            # must not hide the order status the customer can still see.
            return fallback
        if not isinstance(raw, dict) or not raw:
            return fallback
        tracking = dict(raw)
        if estado == "ENTREGADO":
            tracking["estado"] = "ENTREGADO"
            tracking["estadoEtiqueta"] = map_order_status_label("ENTREGADO")
            hitos = tracking.get("hitos")
            if isinstance(hitos, list) and not any(
                str(hito.get("titulo", "")).strip().casefold() == "entregado"
                for hito in hitos
                if isinstance(hito, dict)
            ):
                delivered_at = next(
                    (
                        str(item.get("fecha"))
                        for item in order.get("historial") or []
                        if isinstance(item, dict) and item.get("estado") == "ENTREGADO"
                    ),
                    "",
                )
                hitos.append(
                    {
                        "titulo": "Entregado",
                        "fecha": delivered_at,
                        "completado": True,
                    }
                )
        elif estado == "ANULADO":
            tracking["estado"] = "ANULADO"
            tracking["estadoEtiqueta"] = map_order_status_label("ANULADO")
        tracking.setdefault("pedidoId", order_id)
        tracking.setdefault("distrito", envio.get("distrito"))
        tracking.setdefault("fechaProgramada", envio.get("fechaEstimadaEntrega"))
        tracking.setdefault("detalleDisponible", True)
        return tracking


def provide_orders_service(session: AsyncSession = Depends(get_session)) -> OrdersService:
    settings = get_settings()
    return OrdersService(
        notifications=build_notification_service(session),
        sales_client=provide_sales_port(settings),
        dispatch=provide_despacho_client(settings),
    )


def provide_outbox_processor(session: AsyncSession) -> OutboxProcessor:
    settings = get_settings()
    return OutboxProcessor(
        outbox_repo=OutboxPostgresAdapter(session),
        order_ref_repo=OrderRefPostgresAdapter(session),
        sales_client=provide_sales_port(settings),
        notification_service=build_notification_service(session),
        uow=SqlAlchemyUnitOfWork(session),
        # With inline sending on, fresh SEND_EMAIL jobs are left to the request that created them.
        email_grace=INLINE_EMAIL_GRACE if settings.notification_send_inline else None,
    )


def build_interpret_and_respond(session: AsyncSession) -> InterpretAndRespondUseCase:
    settings = get_settings()
    return InterpretAndRespondUseCase(
        conversations=ConversationPostgresAdapter(session),
        messages=MessagePostgresAdapter(session),
        uow=SqlAlchemyUnitOfWork(session),
        llm=provide_llm_provider(settings),
        tools=provide_tool_executor(session),
        assistant_name=settings.assistant_name,
        llm_timeout_seconds=settings.llm_timeout_seconds,
        sanitize_text=sanitize_sensitive_text,
    )


def provide_interpret_and_respond(
    session: AsyncSession = Depends(get_session),
) -> InterpretAndRespondUseCase:
    return build_interpret_and_respond(session)


@asynccontextmanager
async def provide_turn_scope() -> AsyncIterator[InterpretAndRespondUseCase]:
    """One database session per use: a WebSocket outlives any request-scoped session, so each turn opens its own."""
    connection.get_engine()
    async with connection.open_session() as session:
        yield build_interpret_and_respond(session)


@asynccontextmanager
async def provide_action_scope() -> AsyncIterator[DispatchDirectActionUseCase]:
    """One database session for a direct UI action; no LLM or SSE is opened."""
    connection.get_engine()
    async with connection.open_session() as session:
        yield DispatchDirectActionUseCase(
            conversations=ConversationPostgresAdapter(session),
            messages=MessagePostgresAdapter(session),
            uow=SqlAlchemyUnitOfWork(session),
            dispatcher=provide_action_dispatcher(session),
            sanitize_text=sanitize_sensitive_text,
        )
