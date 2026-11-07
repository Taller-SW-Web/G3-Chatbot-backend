"""Checkout and payment use cases (SPEC-12, SPEC-14)."""
import inspect
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from src.adapters.outbound.pagos.payment_simulator_adapter import CardValidationError
from src.adapters.outbound.persistence.checkout_postgres_adapter import CheckoutPostgresAdapter
from src.adapters.outbound.persistence.models.checkout import Checkout
from src.adapters.outbound.persistence.models.payment_attempt import PaymentAttempt
from src.adapters.outbound.persistence.payment_attempt_postgres_adapter import (
    PaymentAttemptPostgresAdapter,
)
from src.application.dto.commerce_contracts_dto import ShippingQuoteRequest
from src.application.use_cases.notification_service import NotificationService
from src.application.use_cases.shipping_quote import (
    build_destination,
    build_quote_lines,
    parse_quote,
)
from src.domain.entities.checkout import CheckoutStatus
from src.domain.entities.payment_attempt import PaymentRejectReason, PaymentResult
from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError
from src.ports.outbound.payment_gateway_port import PaymentGatewayPort

logger = logging.getLogger(__name__)
DOCUMENT_PATTERNS = {
    "DNI": re.compile(r"[0-9]{8}"),
    "RUC": re.compile(r"(?:10|15|17|20)[0-9]{9}"),
    "CE": re.compile(r"[a-zA-Z0-9]{8,12}"),
    "PASAPORTE": re.compile(r"[a-zA-Z0-9]{6,12}"),
}


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
        notification_service: NotificationService | None = None,
        sales_client: Any | None = None,
        outbox: Any | None = None,
        dispatch: Any | None = None,
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
        self.notification_service = notification_service
        self.sales_client = sales_client
        self.outbox = outbox
        self.dispatch = dispatch

    async def create_checkout(self, input_data: CreateCheckoutInput) -> Checkout:
        if not input_data.idempotency_key:
            raise CheckoutError("VALIDACION", "Se requiere Idempotency-Key", {"campo": "Idempotency-Key"})

        contacto = input_data.contacto
        for field_name in ("nombreCompleto", "tipoDocumento", "numeroDocumento"):
            if not isinstance(contacto, dict) or not isinstance(contacto.get(field_name), str) or not contacto[field_name].strip():
                raise CheckoutError("DATO_INVALIDO", "Los datos de contacto son obligatorios", {"campo": f"contacto.{field_name}"})
        for field_name in ("telefono", "email"):
            if field_name in contacto and not isinstance(contacto[field_name], str):
                raise CheckoutError("DATO_INVALIDO", "El dato de contacto debe ser texto", {"campo": f"contacto.{field_name}"})
        pattern = DOCUMENT_PATTERNS.get(contacto["tipoDocumento"])
        if pattern is None or pattern.fullmatch(contacto["numeroDocumento"]) is None:
            raise CheckoutError("DATO_INVALIDO", "El documento no tiene un formato válido", {"campo": "contacto.numeroDocumento"})

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
            if cart is None or getattr(cart, "customer_id", None) != input_data.customer_id:
                raise CheckoutError("RECURSO_NO_ENCONTRADO", "Carrito no encontrado")
        elif self.carts:
            cart = await _safe_call(self.carts, "obtener_abierto_por_cliente", input_data.customer_id)
            if not cart or not getattr(cart, "id", None):
                raise CheckoutError("CARRITO_VACIO", "No se encontró un carrito activo para iniciar checkout")
            cart_id = cart.id
        else:
            raise CheckoutError("CARRITO_VACIO", "No se encontró un carrito activo para iniciar checkout")

        # 3. Comprobar checkout vivo por carrito garantizando pertenencia
        existing_cart_chk = await _safe_call(
            self.checkouts, "obtener_vivo_por_cart", cart_id, customer_id=input_data.customer_id
        )
        if existing_cart_chk and isinstance(existing_cart_chk, Checkout):
            if existing_cart_chk.customer_id != input_data.customer_id:
                raise CheckoutError("RECURSO_NO_ENCONTRADO", "Checkout no encontrado")
            if existing_cart_chk.idempotency_key == input_data.idempotency_key:
                return existing_cart_chk
            # A retry under a new key while a checkout is alive: reuse it if nothing changed,
            # otherwise retire it, because only one live checkout per cart can be stored.
            existing_summary = existing_cart_chk.summary if isinstance(existing_cart_chk.summary, dict) else {}
            if (
                existing_cart_chk.status != CheckoutStatus.PENDING_PAYMENT.value
                or (
                    existing_summary.get("direccion") == (input_data.direccion or {})
                    and existing_summary.get("contacto") == (input_data.contacto or {})
                )
            ):
                return existing_cart_chk
            await _safe_call(
                self.checkouts, "marcar_estado", existing_cart_chk.id, CheckoutStatus.EXPIRED.value
            )

        # 4. Validar celular verificado si existe el verificador
        if self.phone_verifications and input_data.contacto and input_data.contacto.get("celular"):
            celular = str(input_data.contacto["celular"])
            verificado = await _safe_call(self.phone_verifications, "existe", input_data.customer_id, celular)
            if verificado is False:
                raise CheckoutError("CELULAR_NO_VERIFICADO", "El celular no ha sido verificado")

        # 5. Cálculo y revalidación de importes en el servidor
        items = await _safe_call(self.cart_items, "listar_por_cart", cart_id)
        if not items:
            raise CheckoutError("CARRITO_VACIO", "Tu carrito no tiene productos disponibles para comprar")
        # Decimal, not float: 329.9 + 79.9 is 409.79999999999995 and would never match the cart snapshot.
        subtotal = float(sum((Decimal(str(it.unit_price_ref)) * it.quantity for it in items), Decimal("0")))
        lineas = [
            {
                "sku": it.sku,
                "nombre": it.name,
                "cantidad": it.quantity,
                "precio": float(it.unit_price_ref),
                "imagen": it.image_url,
            }
            for it in items
        ]
        quote = await self._quote_shipping(input_data, items)
        costo_envio = float(quote.costoEnvio)

        # The discount comes only from the cart snapshot, and only while that
        # snapshot still prices the current lines; otherwise the client must
        # confirm the recalculated summary (no order is created).
        totals_snapshot = cart.totals_snapshot if isinstance(cart.totals_snapshot, dict) else {}
        descuento = 0.0
        if totals_snapshot.get("subtotal") is not None:
            if Decimal(str(totals_snapshot["subtotal"])) != Decimal(str(subtotal)):
                raise CheckoutError(
                    "CARRITO_DESACTUALIZADO",
                    "Tu carrito cambió. Revisa el resumen actualizado antes de continuar.",
                    {"summary": self._stale_summary(items, subtotal, costo_envio)},
                )
            discounts = totals_snapshot.get("descuentos", [])
            if isinstance(discounts, list):
                descuento = sum(
                    float(discount.get("amount", 0)) for discount in discounts if isinstance(discount, dict)
                )
        descuento = min(max(descuento, 0.0), subtotal)
        total_final = round(subtotal - descuento + costo_envio, 2)

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=15)
        order_id = f"PED-{now.year}-{uuid.uuid4().hex[:6].upper()}"

        if self.sales_client:
            from src.domain.services.snapshot_builder import SnapshotBuilder, SnapshotValidationError
            from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError

            snapshot_items = [
                {
                    "productoId": str(getattr(it, "product_id", "") or it.sku),
                    "sku": it.sku,
                    "descripcion": it.name,
                    "cantidad": it.quantity,
                    "precioUnitario": float(it.unit_price_ref),
                }
                for it in items
            ]
            contacto_data = input_data.contacto or {}
            direccion_data = input_data.direccion or {}
            snapshot_envio = {
                "modalidad": "DELIVERY",
                "costo": float(costo_envio),
                "destinatario": contacto_data.get("nombreCompleto", ""),
                "departamento": direccion_data.get("departamento", "Lima"),
                "provincia": direccion_data.get("provincia", "Lima"),
                "distrito": direccion_data.get("distrito", ""),
                "direccion": direccion_data.get("direccionExacta", direccion_data.get("direccion", "")),
                "referencia": direccion_data.get("referencia", ""),
            }
            snapshot_pago = {
                "metodoPago": "TARJETA_CREDITO",
                "moneda": "PEN",
                "subtotal": float(subtotal),
                "descuentoCupon": float(descuento),
                "costoEnvio": float(costo_envio),
                "total": float(total_final),
            }
            cupon_data = (
                {"codigo": "CUPON", "descuento": float(descuento), "aplicado": True}
                if descuento > 0
                else None
            )

            try:
                snapshot = SnapshotBuilder.build_snapshot(
                    cliente_id=str(input_data.customer_id),
                    contacto=contacto_data,
                    items=snapshot_items,
                    envio=snapshot_envio,
                    pago=snapshot_pago,
                    cupon=cupon_data,
                )
                ventas_res = await self.sales_client.create_order(
                    snapshot,
                    idempotency_key=input_data.idempotency_key,
                )
                if isinstance(ventas_res, dict) and ventas_res.get("pedidoId"):
                    order_id = str(ventas_res["pedidoId"])
            except SnapshotValidationError as exc:
                # The lines or amounts we computed cannot form a valid order: the cart must be
                # reviewed again, so reuse 409 CARRITO_DESACTUALIZADO with the server summary.
                logger.warning("Order snapshot rejected for cart %s: %s", cart_id, exc)
                raise CheckoutError(
                    "CARRITO_DESACTUALIZADO",
                    "No pudimos validar tu carrito. Revisa el resumen actualizado antes de continuar.",
                    {"summary": self._stale_summary(items, subtotal, costo_envio)},
                ) from exc
            except ModuleRequestError as exc:
                if exc.status == 409:
                    raise CheckoutError(
                        "STOCK_INSUFICIENTE",
                        "Uno o más productos ya no cuentan con stock suficiente",
                    ) from exc
                if exc.status == 400:
                    raise CheckoutError(
                        "DATO_INVALIDO",
                        "Los datos del pedido son inválidos según el módulo de Ventas",
                    ) from exc
                raise CheckoutError(
                    "ERROR_VENTAS",
                    f"Ventas rechazó la creación del pedido: {exc.code}",
                ) from exc
            except ModuleUnavailableError as exc:
                raise CheckoutError(
                    "MODULO_NO_DISPONIBLE",
                    "El servicio de Ventas no está disponible en este momento",
                    {"modulo": "sales"},
                ) from exc

        summary = {
            "lineas": lineas,
            "subtotal": round(subtotal, 2),
            "costoEnvio": round(costo_envio, 2),
            "descuento": round(descuento, 2),
            "total": round(total_final, 2),
            "moneda": quote.moneda,
            "plazoEstimadoDias": quote.plazoEstimadoDias,
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

    async def _quote_shipping(self, input_data: CreateCheckoutInput, items: list[Any]):
        """Server-side Despacho quote for the cart lines (`SPEC-14` Req. 2).

        Raises `SIN_COBERTURA` for an uncovered district and
        `MODULO_NO_DISPONIBLE` when Despacho fails or omits cost or lead time.
        """
        request = self._build_quote_request(input_data.direccion, input_data.contacto)
        try:
            if self.dispatch is None:
                raise ModuleUnavailableError("dispatch", "Dispatch port not configured")
            raw = await self.dispatch.quote_shipping(
                destino=build_destination(request),
                lineas=build_quote_lines(items),
                correlation_id=str(uuid.uuid4()),
            )
            quote = parse_quote(raw, request.distrito)
        except (ModuleUnavailableError, ModuleRequestError) as exc:
            logger.warning("Despacho quote failed during checkout: %s", exc)
            raise CheckoutError(
                "MODULO_NO_DISPONIBLE",
                "No pudimos calcular el envío ahora. Intenta de nuevo en unos segundos.",
                {"modulo": "dispatch"},
            ) from exc
        if quote is None:
            raise CheckoutError(
                "SIN_COBERTURA",
                f"Aún no llegamos a {request.distrito}.",
                {"distrito": request.distrito},
            )
        return quote

    @staticmethod
    def _build_quote_request(direccion: Any, contacto: dict[str, Any] | None) -> ShippingQuoteRequest:
        if not isinstance(direccion, dict):
            raise CheckoutError(
                "VALIDACION",
                "La dirección de envío es obligatoria",
                {"errores": [{"campo": "direccion", "mensaje": "La dirección es obligatoria."}]},
            )
        recipient = direccion.get("destinatario") or (contacto or {}).get("nombreCompleto")
        fields = {
            "destinatario": recipient,
            "direccion": direccion.get("direccion") or direccion.get("direccionExacta"),
            "distrito": direccion.get("distrito"),
            "departamento": direccion.get("departamento"),
            "provincia": direccion.get("provincia"),
            "referencia": direccion.get("referencia"),
        }
        try:
            return ShippingQuoteRequest(**{k: v for k, v in fields.items() if v is not None})
        except ValidationError as exc:
            errores = [
                {
                    "campo": ".".join(["direccion", *(str(p) for p in err["loc"])]),
                    "mensaje": err["msg"].removeprefix("Value error, "),
                }
                for err in exc.errors()
            ]
            raise CheckoutError(
                "VALIDACION", "La dirección de envío está incompleta", {"errores": errores}
            ) from exc

    @staticmethod
    def _stale_summary(items: list[Any], subtotal: float, costo_envio: float) -> dict[str, Any]:
        """Server-computed summary returned with `409 CARRITO_DESACTUALIZADO`."""
        return {
            "lineas": [
                {"sku": it.sku, "cantidad": it.quantity, "precioUnitario": float(it.unit_price_ref)}
                for it in items
            ],
            "subtotal": round(subtotal, 2),
            "descuento": 0.0,
            "costoEnvio": round(costo_envio, 2),
            "total": round(subtotal + costo_envio, 2),
        }

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
            existing_checkout = await self.checkouts.obtener_por_id(existing_attempt.checkout_id)
            if not existing_checkout or existing_checkout.customer_id != input_data.customer_id:
                raise CheckoutError("RECURSO_NO_ENCONTRADO", "Checkout no encontrado")
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
                    if self.outbox:
                        from src.adapters.outbound.persistence.models.outbox import Outbox

                        outbox_entry = Outbox(
                            type="REQUEST_CANCELLATION",
                            payload={
                                "order_id": ord_row.order_id,
                                "motivo": "PAGO_NO_COMPLETADO",
                                "comentario": "Checkout expirado por tiempo límite",
                            },
                            status="PENDING",
                            attempts=0,
                        )
                        await _safe_call(self.outbox, "guardar", outbox_entry)
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

            # Encolar notificación a Ventas en outbox (SPEC-15 Req. 2)
            if order_id and self.outbox:
                from src.adapters.outbound.persistence.models.outbox import Outbox

                payment_payload = {
                    "transaccionId": payment_res.transaction_id or f"SIM-{uuid.uuid4().hex[:8].upper()}",
                    "resultado": "APROBADO",
                    "monto": float(row.total),
                    "moneda": "PEN",
                    "metodo": "TARJETA_CREDITO",
                    "marcaTarjeta": payment_res.brand or "VISA",
                    "ultimosCuatroDigitos": payment_res.last4 or "••••",
                    "fechaPago": datetime.now(timezone.utc).isoformat(),
                    "codigoAutorizacion": f"AUTH-{uuid.uuid4().hex[:6].upper()}",
                }
                outbox_entry = Outbox(
                    type="NOTIFY_SALES_PAYMENT",
                    payload={
                        "order_id": order_id,
                        "payment_data": payment_payload,
                    },
                    status="PENDING",
                    attempts=0,
                )
                await _safe_call(self.outbox, "guardar", outbox_entry)

            confirmation_res = None
            if order_id:
                if ord_row and getattr(ord_row, "order_id", None):
                    await _safe_call(self.orders, "actualizar_estado", order_id, "PAID_NOTIFIED")

                if self.notification_service is not None:
                    try:
                        confirmation_res = await self.notification_service.enqueue_confirmation(order_id)
                    except Exception:
                        logger.exception("Failed to enqueue confirmation for order %s", order_id)

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
                if self.outbox:
                    from src.adapters.outbound.persistence.models.outbox import Outbox

                    outbox_entry = Outbox(
                        type="REQUEST_CANCELLATION",
                        payload={
                            "order_id": ord_row.order_id,
                            "motivo": "PAGO_NO_COMPLETADO",
                            "comentario": "Intentos de pago con tarjeta agotados (3 rechazos)",
                        },
                        status="PENDING",
                        attempts=0,
                    )
                    await _safe_call(self.outbox, "guardar", outbox_entry)

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
