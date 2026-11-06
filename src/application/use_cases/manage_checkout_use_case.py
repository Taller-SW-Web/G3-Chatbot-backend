"""Checkout and payment use cases (SPEC-12, SPEC-14)."""
import inspect
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from src.adapters.outbound.pagos.payment_simulator_adapter import CardValidationError
from src.adapters.outbound.persistence.checkout_postgres_adapter import CheckoutPostgresAdapter
from src.adapters.outbound.persistence.models.checkout import Checkout
from src.adapters.outbound.persistence.models.payment_attempt import PaymentAttempt
from src.adapters.outbound.persistence.payment_attempt_postgres_adapter import (
    PaymentAttemptPostgresAdapter,
)
from src.domain.entities.checkout import CheckoutStatus
from src.domain.entities.payment_attempt import PaymentRejectReason, PaymentResult
from src.ports.outbound.payment_gateway_port import PaymentGatewayPort

logger = logging.getLogger(__name__)


async def _safe_call(obj: Any, method_name: str, *args: Any, **kwargs: Any) -> Any:
    """Invokes method if present and awaits if returned object is awaitable."""
    if obj is None:
        return None
    method = getattr(obj, method_name, None)
    if not callable(method):
        return None
    res = method(*args, **kwargs)
    if inspect.isawaitable(res):
        return await res
    return res


class CheckoutError(Exception):
    def __init__(self, code: str, detail: str = "", extra: dict[str, Any] | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.extra = extra or {}


@dataclass
class CreateCheckoutInput:
    customer_id: uuid.UUID
    idempotency_key: str
    cart_id: uuid.UUID | None = None
    direccion: dict[str, Any] | None = None
    contacto: dict[str, Any] | None = None
    lineas: list[dict[str, Any]] = field(default_factory=list)
    subtotal: float = 0.0
    costo_envio: float | None = None
    descuento: float = 0.0
    total: float = 0.0


@dataclass
class ProcessPaymentInput:
    checkout_id: uuid.UUID
    customer_id: uuid.UUID
    idempotency_key: str
    numero_tarjeta: str
    titular: str
    vencimiento: str
    cvv: str
    access_token: str | None = None


class ManageCheckoutUseCase:
    def __init__(
        self,
        checkouts: CheckoutPostgresAdapter,
        payment_attempts: PaymentAttemptPostgresAdapter,
        payment_gateway: PaymentGatewayPort,
        uow: Any | None = None,
        security_client: Any | None = None,
        carts: Any | None = None,
        cart_items: Any | None = None,
        orders: Any | None = None,
        phone_verifications: Any | None = None,
        session: Any | None = None,
        notification_service: Any | None = None,
    ):
        self.checkouts = checkouts
        self.payment_attempts = payment_attempts
        self.payment_gateway = payment_gateway
        self.uow = uow
        self.security_client = security_client
        self.carts = carts
        self.cart_items = cart_items
        self.orders = orders
        self.phone_verifications = phone_verifications
        self.session = session
        self.notification_service = notification_service

    async def create_checkout(self, input_data: CreateCheckoutInput) -> Checkout:
        if not input_data.idempotency_key:
            raise CheckoutError("VALIDACION", "Se requiere Idempotency-Key", {"campo": "Idempotency-Key"})

        # 1. Idempotencia real previa por clave
        existing = await _safe_call(self.checkouts, "obtener_por_idempotency_key", input_data.idempotency_key)
        if existing and isinstance(existing, Checkout):
            if existing.customer_id != input_data.customer_id:
                raise CheckoutError("CONFLICTO", "Idempotency-Key ya usada por otro usuario")
            return existing

        # 2. Resolución del carrito sin generar cartId ficticio que viole FK
        cart_id = input_data.cart_id
        if cart_id is not None:
            cart = await _safe_call(self.carts, "obtener_por_id", cart_id)
            if cart and getattr(cart, "customer_id", None) and cart.customer_id != input_data.customer_id:
                raise CheckoutError("RECURSO_NO_ENCONTRADO", "Carrito no encontrado")
        elif self.carts:
            cart = await _safe_call(self.carts, "obtener_abierto_por_cliente", input_data.customer_id)
            if not cart or not getattr(cart, "id", None):
                raise CheckoutError("CARRITO_VACIO", "No se encontró un carrito activo para iniciar checkout")
            cart_id = cart.id
        else:
            if not cart_id:
                raise CheckoutError("VALIDACION", "Se requiere cartId válido", {"campo": "cartId"})

        # 3. Comprobar checkout vivo por carrito garantizando pertenencia
        existing_cart_chk = await _safe_call(
            self.checkouts, "obtener_vivo_por_cart", cart_id, customer_id=input_data.customer_id
        )
        if existing_cart_chk and isinstance(existing_cart_chk, Checkout):
            if existing_cart_chk.customer_id != input_data.customer_id:
                raise CheckoutError("RECURSO_NO_ENCONTRADO", "Checkout no encontrado")
            if existing_cart_chk.idempotency_key == input_data.idempotency_key:
                return existing_cart_chk

        # 4. Validar celular verificado si existe el verificador
        if self.phone_verifications and input_data.contacto and input_data.contacto.get("celular"):
            celular = str(input_data.contacto["celular"])
            verificado = await _safe_call(self.phone_verifications, "existe", input_data.customer_id, celular)
            if verificado is False:
                raise CheckoutError("CELULAR_NO_VERIFICADO", "El celular no ha sido verificado")

        # 5. Cálculo y revalidación de importes en el servidor
        lineas = input_data.lineas
        subtotal = float(input_data.subtotal)
        items = await _safe_call(self.cart_items, "listar_por_cart", cart_id)
        if items and isinstance(items, list):
            subtotal = float(sum(float(getattr(it, "unit_price_ref", 0)) * getattr(it, "quantity", 1) for it in items))
            lineas = [
                {
                    "sku": getattr(it, "sku", ""),
                    "nombre": getattr(it, "name", ""),
                    "cantidad": getattr(it, "quantity", 1),
                    "precio": float(getattr(it, "unit_price_ref", 0)),
                    "imagen": getattr(it, "image_url", None),
                }
                for it in items
            ]
        elif self.cart_items and not lineas:
            raise CheckoutError("CARRITO_VACIO", "Tu carrito no tiene productos disponibles para comprar")

        costo_envio = float(input_data.costo_envio if input_data.costo_envio is not None else (12.50 if input_data.direccion else 0.0))
        descuento = float(input_data.descuento or 0.0)
        total_servidor = round(subtotal - descuento + costo_envio, 2)

        # Si el cliente envió un total y difiere de lo calculado en servidor, rechazar con 409
        if input_data.total > 0 and total_servidor > 0 and abs(float(input_data.total) - total_servidor) > 0.01:
            raise CheckoutError(
                "CARRITO_DESACTUALIZADO",
                "El total del carrito ha cambiado. Por favor confirma el nuevo resumen.",
                {
                    "total": total_servidor,
                    "subtotal": round(subtotal, 2),
                    "costoEnvio": round(costo_envio, 2),
                    "descuento": round(descuento, 2),
                },
            )

        total_final = total_servidor if total_servidor > 0 else float(input_data.total)

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=15)
        order_id = f"PED-{now.year}-{uuid.uuid4().hex[:6].upper()}"

        summary = {
            "lineas": lineas,
            "subtotal": round(subtotal, 2),
            "costoEnvio": round(costo_envio, 2),
            "descuento": round(descuento, 2),
            "total": round(total_final, 2),
            "direccion": input_data.direccion or {},
            "contacto": input_data.contacto or {},
            "orderId": order_id,
        }

        checkout_row = Checkout(
            cart_id=cart_id,
            customer_id=input_data.customer_id,
            status=CheckoutStatus.PENDING_PAYMENT.value,
            summary=summary,
            total=total_final,
            payment_attempts=0,
            expires_at=expires_at,
            idempotency_key=input_data.idempotency_key,
        )

        saved = await self.checkouts.guardar(checkout_row)

        # Actualizar estado del carrito a IN_CHECKOUT
        await _safe_call(self.carts, "cambiar_estado", cart_id, "IN_CHECKOUT")

        # Registrar referencia del pedido en Ventas (SPEC-14, SPEC-15)
        if self.orders:
            from src.adapters.outbound.persistence.models.order_ref import OrderRef

            try:
                await _safe_call(
                    self.orders,
                    "guardar",
                    OrderRef(
                        order_id=order_id,
                        customer_id=input_data.customer_id,
                        checkout_id=saved.id,
                        local_status="CREATED",
                        total=saved.total,
                    ),
                )
            except Exception:
                pass

        if self.uow:
            await _safe_call(self.uow, "commit")
        return saved

    async def get_checkout(self, checkout_id: uuid.UUID, customer_id: uuid.UUID) -> Checkout:
        row = await self.checkouts.obtener_por_id(checkout_id)
        if not row or row.customer_id != customer_id:
            raise CheckoutError("RECURSO_NO_ENCONTRADO", "Checkout no encontrado")

        now = datetime.now(timezone.utc)
        if row.status == CheckoutStatus.PENDING_PAYMENT.value and now > row.expires_at:
            await self.checkouts.marcar_estado(row.id, CheckoutStatus.EXPIRED.value)
            await _safe_call(self.carts, "cambiar_estado", row.cart_id, "ACTIVE")
            ord_row = await _safe_call(self.orders, "obtener_por_checkout_id", row.id)
            if ord_row and getattr(ord_row, "order_id", None):
                await _safe_call(self.orders, "actualizar_estado", ord_row.order_id, "CANCELLED")
            if self.uow:
                await _safe_call(self.uow, "commit")
            row.status = CheckoutStatus.EXPIRED.value

        return row

    async def process_payment(self, input_data: ProcessPaymentInput) -> PaymentAttempt:
        if not input_data.idempotency_key:
            raise CheckoutError("VALIDACION", "Se requiere Idempotency-Key", {"campo": "Idempotency-Key"})

        # 1. Idempotencia real: buscar por clave antes de ejecutar
        existing_attempt = await _safe_call(self.payment_attempts, "obtener_por_idempotency_key", input_data.idempotency_key)
        if existing_attempt and isinstance(existing_attempt, PaymentAttempt):
            if existing_attempt.checkout_id != input_data.checkout_id:
                raise CheckoutError("CONFLICTO", "Idempotency-Key ya usada en otro checkout")
            return existing_attempt

        row = await self.checkouts.obtener_por_id(input_data.checkout_id)
        if not row or row.customer_id != input_data.customer_id:
            raise CheckoutError("RECURSO_NO_ENCONTRADO", "Checkout no encontrado")

        now = datetime.now(timezone.utc)
        if row.status == CheckoutStatus.EXPIRED.value or (
            row.status == CheckoutStatus.PENDING_PAYMENT.value and now > row.expires_at
        ):
            if row.status != CheckoutStatus.EXPIRED.value:
                await self.checkouts.marcar_estado(row.id, CheckoutStatus.EXPIRED.value)
                await _safe_call(self.carts, "cambiar_estado", row.cart_id, "ACTIVE")
                ord_row = await _safe_call(self.orders, "obtener_por_checkout_id", row.id)
                if ord_row and getattr(ord_row, "order_id", None):
                    await _safe_call(self.orders, "actualizar_estado", ord_row.order_id, "CANCELLED")
                if self.uow:
                    await _safe_call(self.uow, "commit")
            raise CheckoutError("CHECKOUT_EXPIRADO", "El checkout venció hace más de 15 minutos")

        if row.status == CheckoutStatus.PAYMENT_APPROVED.value:
            attempts = await self.payment_attempts.listar_por_checkout(row.id)
            approved = next((a for a in attempts if getattr(a, "result", None) == PaymentResult.APPROVED.value), None)
            if approved:
                return approved

        if row.status == CheckoutStatus.FAILED.value or (row.payment_attempts or 0) >= 3:
            raise CheckoutError(
                "PAGO_RECHAZADO",
                "Se agotaron los intentos de pago permitidos",
                {"motivo": "INTENTOS_AGOTADOS", "intentosRestantes": 0},
            )

        # 2. Introspección previa obligatoria con token real del cliente y fallo en cerrado (SPEC-14 Req. 4)
        if self.security_client and hasattr(self.security_client, "introspect"):
            token_to_verify = input_data.access_token or ""
            if not token_to_verify:
                raise CheckoutError("REQUIERE_SESION", "Token de sesión requerido para procesar el pago")
            try:
                intro = await _safe_call(self.security_client, "introspect", token_to_verify)
                is_active = False
                if isinstance(intro, dict):
                    is_active = bool(intro.get("activo", intro.get("active", False)))
                if not is_active:
                    raise CheckoutError("TOKEN_INVALIDO", "Tu sesión ya no es válida, vuelve a iniciar sesión")
            except CheckoutError:
                raise
            except Exception as exc:
                logger.warning("Fallo en introspección de sesión (fail-closed): %s", exc)
                raise CheckoutError(
                    "SERVICIO_NO_DISPONIBLE",
                    "No pudimos confirmar tu sesión, intenta de nuevo en un momento",
                ) from exc

        # 3. Validación y simulación de la tarjeta
        try:
            payment_res = await self.payment_gateway.process_payment(
                card_number=input_data.numero_tarjeta,
                holder_name=input_data.titular,
                expiry=input_data.vencimiento,
                cvv=input_data.cvv,
                amount=float(row.total),
            )
        except CardValidationError as exc:
            raise CheckoutError("VALIDACION", exc.message, {"campo": exc.field}) from exc

        # 4. Caso aprobado
        if payment_res.result == PaymentResult.APPROVED.value:
            await self.checkouts.marcar_estado(row.id, CheckoutStatus.PAYMENT_APPROVED.value)
            await _safe_call(self.carts, "cambiar_estado", row.cart_id, "CONVERTED")
            ord_row = await _safe_call(self.orders, "obtener_por_checkout_id", row.id)
            order_id = getattr(ord_row, "order_id", None) if ord_row else None
            if not order_id and isinstance(row.summary, dict):
                order_id = row.summary.get("orderId")

            confirmation_res = None
            if order_id:
                if ord_row and getattr(ord_row, "order_id", None):
                    await _safe_call(self.orders, "actualizar_estado", order_id, "PAID_NOTIFIED")

                session = self.session or (getattr(self.uow, "session", None) if self.uow else None)
                try:
                    if self.notification_service is not None:
                        confirmation_res = await _safe_call(
                            self.notification_service, "enqueue_confirmation", order_id
                        )
                    elif session is not None:
                        try:
                            from src.infrastructure.config.container import build_notification_service

                            notif_svc = build_notification_service(session)
                            confirmation_res = await notif_svc.enqueue_confirmation(order_id)
                        except (ImportError, AttributeError):
                            pass
                except Exception as exc:
                    logger.warning("Fallo al encolar confirmación para pedido %s: %s", order_id, exc)

            attempt = PaymentAttempt(
                checkout_id=row.id,
                idempotency_key=input_data.idempotency_key,
                result=PaymentResult.APPROVED.value,
                brand=payment_res.brand,
                last4=payment_res.last4,
                amount=row.total,
                transaction_id=payment_res.transaction_id,
            )
            saved_attempt = await self.payment_attempts.guardar(attempt)
            if confirmation_res:
                saved_attempt.confirmation = confirmation_res
            if self.uow:
                await _safe_call(self.uow, "commit")
            return saved_attempt

        # 5. Caso rechazado o error
        await self.checkouts.incrementar_intentos(row.id)
        current_attempts = (row.payment_attempts or 0) + 1
        remaining = max(0, 3 - current_attempts)

        if current_attempts >= 3:
            await self.checkouts.marcar_estado(row.id, CheckoutStatus.FAILED.value)
            await _safe_call(self.carts, "cambiar_estado", row.cart_id, "ACTIVE")
            ord_row = await _safe_call(self.orders, "obtener_por_checkout_id", row.id)
            if ord_row and getattr(ord_row, "order_id", None):
                await _safe_call(self.orders, "actualizar_estado", ord_row.order_id, "CANCELLED")

        attempt = PaymentAttempt(
            checkout_id=row.id,
            idempotency_key=input_data.idempotency_key,
            result=payment_res.result,
            brand=payment_res.brand,
            last4=payment_res.last4,
            amount=row.total,
            reason=payment_res.reason,
        )
        await self.payment_attempts.guardar(attempt)
        if self.uow:
            await _safe_call(self.uow, "commit")

        motivo_map = {
            PaymentRejectReason.INSUFFICIENT_FUNDS.value: "FONDOS_INSUFICIENTES",
            PaymentRejectReason.DECLINED_BY_ISSUER.value: "RECHAZADO_POR_EMISOR",
            PaymentRejectReason.PROCESSING_ERROR.value: "ERROR_PROCESAMIENTO",
        }
        motivo_es = motivo_map.get(payment_res.reason or "", payment_res.reason or "RECHAZADO")

        if payment_res.result == PaymentResult.ERROR.value:
            raise CheckoutError(
                "ERROR_PROCESAMIENTO",
                "Ocurrió un error temporal al procesar el pago. Intente de nuevo.",
                {"motivo": motivo_es, "intentosRestantes": remaining},
            )

        raise CheckoutError(
            "PAGO_RECHAZADO",
            "Tu tarjeta fue rechazada. Puedes intentar con otra tarjeta.",
            {"motivo": motivo_es, "intentosRestantes": remaining},
        )
