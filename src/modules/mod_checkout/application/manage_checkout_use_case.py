"""Checkout and payment use cases (SPEC-12, SPEC-14)."""
import asyncio
import inspect
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from src.modules.mod_checkout.adapters.payment_simulator_adapter import CardValidationError
from src.modules.mod_checkout.domain.checkout import CheckoutStatus, NewCheckout
from src.modules.mod_checkout.domain.payment_attempt import (
    NewPaymentAttempt,
    PaymentConflictError,
    PaymentRejectReason,
    PaymentResult,
)
from src.modules.mod_checkout.ports.payment_gateway_port import PaymentGatewayPort
from src.modules.mod_shipping.public import (
    build_destination,
    build_quote_lines,
    parse_quote,
)
from src.shared.application.dto.commerce_contracts_dto import ShippingQuoteRequest
from src.shared.application.dto.order_ref_dto import NewOrderReference
from src.shared.application.dto.outbox_task_dto import NewOutboxTask
from src.shared.domain.masking import mask_phone
from src.shared.domain.module_errors import (
    ModuleRequestError,
    ModuleUnavailableError,
    failure_summary,
)
from src.shared.domain.request_context import correlation_id_var

logger = logging.getLogger(__name__)
DOCUMENT_PATTERNS = {
    "DNI": re.compile(r"[0-9]{8}"),
    "RUC": re.compile(r"(?:10|15|17|20)[0-9]{9}"),
    "CE": re.compile(r"[a-zA-Z0-9]{8,12}"),
    "PASAPORTE": re.compile(r"[a-zA-Z0-9]{6,12}"),
}
VERIFIED_CONTACT_MESSAGE = (
    "No pudimos confirmar el correo de tu cuenta. "
    "Revisa tus datos en Mi cuenta o inténtalo de nuevo."
)
# Internal `checkout.summary` keys used to reconcile a Sales order after a timeout.
# The HTTP layer strips every `_`-prefixed key before exposing the summary.
SALES_PENDING_KEY = "_salesPending"
SALES_SNAPSHOT_KEY = "_salesSnapshot"


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
    access_token: str | None = None
    guardar_direccion: bool = False


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
        checkouts: Any,
        payment_attempts: Any,
        payment_gateway: PaymentGatewayPort,
        uow: Any | None = None,
        security_client: Any | None = None,
        carts: Any | None = None,
        cart_items: Any | None = None,
        orders: Any | None = None,
        phone_verifications: Any | None = None,
        sales_client: Any | None = None,
        sales_deadline_seconds: float = 30.0,
        outbox: Any | None = None,
        dispatch: Any | None = None,
        require_phone_verification: bool = False,
        cart_validator: Any | None = None,
        cart_totals: Any | None = None,
        save_address_enabled: bool = True,
        require_verified_contact: bool = False,
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
        self.sales_client = sales_client
        self.sales_deadline_seconds = sales_deadline_seconds
        self.outbox = outbox
        self.dispatch = dispatch
        self.require_phone_verification = require_phone_verification
        self.cart_validator = cart_validator
        self.cart_totals = cart_totals
        self.save_address_enabled = save_address_enabled
        self.require_verified_contact = require_verified_contact

    @staticmethod
    def _is_reusable(
        checkout: Any, input_data: CreateCheckoutInput, lineas: list[dict[str, Any]], subtotal: float
    ) -> bool:
        """True when a pending checkout still matches the address, contact and cart content."""
        summary = checkout.summary if isinstance(checkout.summary, dict) else {}

        def signature(lines: Any) -> list[tuple[str, int, Decimal]]:
            return sorted(
                (str(line.get("sku")), int(line.get("cantidad", 0)), Decimal(str(line.get("precio", 0))))
                for line in lines
                if isinstance(line, dict)
            )

        try:
            return (
                summary.get("direccion") == (input_data.direccion or {})
                and summary.get("contacto") == (input_data.contacto or {})
                and signature(summary.get("lineas") or []) == signature(lineas)
                and Decimal(str(summary.get("subtotal"))) == Decimal(str(round(subtotal, 2)))
            )
        except (ArithmeticError, ValueError, TypeError):
            return False

    async def _ensure_phone_verified(self, input_data: CreateCheckoutInput) -> None:
        """SPEC-04 Req. 1: exact-match between today's phone and the local verification."""
        if not self.require_phone_verification:
            return
        if self.security_client is None or not input_data.access_token:
            raise CheckoutError(
                "CELULAR_NO_VERIFICADO",
                "No pudimos confirmar tu celular ahora.",
                {"motivo": "SIN_SESION"},
            )
        try:
            profile = dict(await self.security_client.me(input_data.access_token))
        except Exception as exc:
            logger.warning("Seguridad profile unavailable while checking the phone: %s", exc)
            raise CheckoutError(
                "CELULAR_NO_VERIFICADO",
                "No pudimos confirmar tu celular ahora.",
                {"motivo": "SEGURIDAD_NO_DISPONIBLE"},
            ) from exc
        phone = profile.get("celular") or profile.get("telefono")
        if not phone:
            raise CheckoutError(
                "CELULAR_NO_VERIFICADO",
                "Tu perfil no tiene un celular registrado.",
                {"motivo": "SIN_CELULAR"},
            )
        phone = str(phone)
        verified = await _safe_call(
            self.phone_verifications, "existe", input_data.customer_id, phone
        )
        if not verified:
            raise CheckoutError(
                "CELULAR_NO_VERIFICADO",
                f"Para coordinar la entrega necesitamos confirmar tu celular {mask_phone(phone)}",
                {"celularEnmascarado": mask_phone(phone)},
            )

    async def _ensure_verified_contact(self, input_data: CreateCheckoutInput) -> None:
        """Default guard: the profile e-mail is the verified contact, a locally verified phone the fallback.

        `REQUIRE_PHONE_VERIFICATION` keeps the legacy phone-only mode and wins when both
        flags are on. The profile e-mail is normalized onto the contact so the summary,
        the order snapshot and the confirmation e-mail all use the verified address.
        """
        if self.require_phone_verification:
            await self._ensure_phone_verified(input_data)
            return
        if not self.require_verified_contact:
            return
        # Defensive: the router already requires `require_customer`, so a request
        # without a token should not reach the guard; never trust an anonymous one.
        if self.security_client is None or not input_data.access_token:
            raise CheckoutError(
                "CONTACTO_NO_VERIFICADO",
                VERIFIED_CONTACT_MESSAGE,
                {"motivo": "SIN_SESION"},
            )
        try:
            profile = dict(await self.security_client.me(input_data.access_token))
        except Exception as exc:
            # A 401/403 from Seguridad means the holder token is no longer valid;
            # anything else (timeout, 5xx, unexpected shape) is an outage.
            motivo = (
                "SIN_SESION"
                if isinstance(exc, ModuleRequestError) and exc.status in (401, 403)
                else "SEGURIDAD_NO_DISPONIBLE"
            )
            logger.warning("Seguridad profile unavailable while checking the contact: %s", exc)
            raise CheckoutError(
                "CONTACTO_NO_VERIFICADO",
                VERIFIED_CONTACT_MESSAGE,
                {"motivo": motivo},
            ) from exc

        contacto = input_data.contacto if isinstance(input_data.contacto, dict) else {}
        raw_email = profile.get("correo") or profile.get("email")
        email = str(raw_email).strip() if raw_email else ""
        if email:
            contacto["email"] = email
            input_data.contacto = contacto
            return

        # The snapshot only carries the profile e-mail: drop any client-supplied
        # address when there is no verified profile address to normalize.
        contacto.pop("email", None)
        input_data.contacto = contacto

        raw_phone = profile.get("celular") or profile.get("telefono")
        phone = str(raw_phone).strip() if raw_phone else ""
        if phone:
            verified = await _safe_call(
                self.phone_verifications, "existe", input_data.customer_id, phone
            )
            if verified:
                return
            raise CheckoutError(
                "CONTACTO_NO_VERIFICADO",
                VERIFIED_CONTACT_MESSAGE,
                {
                    "motivo": "SIN_CONTACTO_VERIFICADO",
                    "celularEnmascarado": mask_phone(phone),
                },
            )
        raise CheckoutError(
            "CONTACTO_NO_VERIFICADO",
            VERIFIED_CONTACT_MESSAGE,
            {"motivo": "SIN_CONTACTO_VERIFICADO"},
        )

    @staticmethod
    def _is_sales_pending(checkout: Any) -> bool:
        """True while the Ventas order is unconfirmed (checkout persisted, call in flight)."""
        summary = getattr(checkout, "summary", None)
        return isinstance(summary, dict) and summary.get(SALES_PENDING_KEY) is True

    async def _create_order_in_sales(self, snapshot: dict[str, Any], idempotency_key: str) -> Any:
        """Create the order in Ventas with an internal deadline shorter than the proxy timeout.

        The checkout is already persisted when this runs, so a deadline hit is an
        uncertain-but-recoverable state: the retry replays the same Idempotency-Key
        and Ventas returns the order it may have created.
        """
        if self.sales_client is None:
            raise ModuleUnavailableError("sales", "sales port not configured")
        try:
            return await asyncio.wait_for(
                self.sales_client.create_order(snapshot, idempotency_key=idempotency_key),
                timeout=self.sales_deadline_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ModuleUnavailableError(
                "sales",
                f"order creation exceeded CHECKOUT_SALES_DEADLINE_SECONDS={self.sales_deadline_seconds:g}",
            ) from exc

    def _sales_unavailable_error(self, checkout: Any, exc: ModuleUnavailableError) -> CheckoutError:
        """Recoverable 503: the client gets the checkout/order ids to retry or query."""
        summary = checkout.summary if isinstance(checkout.summary, dict) else {}
        timed_out = isinstance(exc.__cause__, TimeoutError)
        return CheckoutError(
            "MODULO_NO_DISPONIBLE",
            "No pudimos confirmar tu pedido con Ventas. No se realizó ningún cobro; "
            "reintenta con la misma Idempotency-Key o consulta el checkout.",
            {
                "modulo": "sales",
                "motivo": "TIMEOUT_VENTAS" if timed_out else "VENTAS_NO_DISPONIBLE",
                "checkoutId": str(checkout.id),
                "orderId": summary.get("orderId"),
                "orderIdProvisional": True,
                "reintentable": True,
            },
        )

    @staticmethod
    def _sales_request_error(exc: ModuleRequestError) -> CheckoutError:
        """A 4xx from Ventas is definitive: no order was created, so the local row is retired."""
        if exc.status == 409:
            return CheckoutError(
                "STOCK_INSUFICIENTE",
                "Uno o más productos ya no cuentan con stock suficiente",
            )
        if exc.status == 400:
            return CheckoutError(
                "DATO_INVALIDO",
                "Los datos del pedido son inválidos según el módulo de Ventas",
            )
        return CheckoutError(
            "ERROR_VENTAS",
            f"Ventas rechazó la creación del pedido: {exc.code}",
        )

    async def _retire_sales_pending(self, checkout: Any) -> None:
        """Free the cart and retire the provisional checkout after a definitive 4xx.

        The row never stays `PENDING_PAYMENT`, so it cannot block a retry with a new key.
        """
        summary = dict(checkout.summary) if isinstance(checkout.summary, dict) else {}
        summary.pop(SALES_PENDING_KEY, None)
        summary.pop(SALES_SNAPSHOT_KEY, None)
        checkout.summary = summary
        await _safe_call(self.checkouts, "marcar_estado", checkout.id, CheckoutStatus.EXPIRED.value)
        await _safe_call(self.carts, "cambiar_estado", checkout.cart_id, "ACTIVE")
        if self.uow:
            await _safe_call(self.uow, "commit")

    async def _apply_sales_order(self, checkout: Any, ventas_res: Any) -> Any:
        """Persist the real `pedidoId`, clear the provisional state and record `order_ref`."""
        order_id = ventas_res.get("pedidoId") if isinstance(ventas_res, dict) else None
        if not order_id:
            raise ModuleUnavailableError("sales", "respuesta sin pedidoId")
        summary = dict(checkout.summary) if isinstance(checkout.summary, dict) else {}
        summary["orderId"] = str(order_id)
        summary.pop(SALES_PENDING_KEY, None)
        summary.pop(SALES_SNAPSHOT_KEY, None)
        checkout.summary = summary
        await _safe_call(self.checkouts, "guardar", checkout)
        if self.orders:
            try:
                await _safe_call(
                    self.orders,
                    "guardar",
                    NewOrderReference(
                        order_id=str(order_id),
                        customer_id=checkout.customer_id,
                        checkout_id=checkout.id,
                        local_status="CREATED",
                        total=checkout.total,
                    ),
                )
            except Exception:
                pass
        if self.uow:
            await _safe_call(self.uow, "commit")
        return checkout

    async def _resume_pending_sale(self, checkout: Any) -> Any:
        """Reconcile an order left pending by a timeout, replaying the same Idempotency-Key."""
        if not self._is_sales_pending(checkout):
            return checkout
        if getattr(checkout, "status", None) != CheckoutStatus.PENDING_PAYMENT.value:
            return checkout
        expires_at = getattr(checkout, "expires_at", None)
        if isinstance(expires_at, datetime) and datetime.now(timezone.utc) > expires_at:
            await self.expire_checkout(checkout.id)
            refreshed = await _safe_call(self.checkouts, "obtener_por_id", checkout.id)
            return refreshed or checkout

        summary = checkout.summary if isinstance(checkout.summary, dict) else {}
        snapshot = summary.get(SALES_SNAPSHOT_KEY)
        if not isinstance(snapshot, dict):
            # Legacy row without the submitted snapshot: never invent a new payload
            # under the same key. The client can query the checkout or retry with a new key.
            raise CheckoutError(
                "MODULO_NO_DISPONIBLE",
                "Estamos confirmando tu pedido. Consulta el checkout antes de reintentar.",
                {
                    "modulo": "sales",
                    "motivo": "RECONCILIACION_NO_DISPONIBLE",
                    "checkoutId": str(checkout.id),
                    "orderId": summary.get("orderId"),
                    "orderIdProvisional": True,
                    "reintentable": True,
                },
            )
        try:
            ventas_res = await self._create_order_in_sales(snapshot, checkout.idempotency_key)
        except ModuleRequestError as exc:
            await self._retire_sales_pending(checkout)
            raise self._sales_request_error(exc) from exc
        except ModuleUnavailableError as exc:
            raise self._sales_unavailable_error(checkout, exc) from exc
        try:
            return await self._apply_sales_order(checkout, ventas_res)
        except ModuleUnavailableError as exc:
            raise self._sales_unavailable_error(checkout, exc) from exc

    async def create_checkout(self, input_data: CreateCheckoutInput) -> Any:
        if not input_data.idempotency_key:
            raise CheckoutError("VALIDACION", "Se requiere Idempotency-Key", {"campo": "Idempotency-Key"})

        contacto = input_data.contacto
        if not isinstance(contacto, dict):
            raise CheckoutError("DATO_INVALIDO", "Los datos de contacto son obligatorios", {"campo": "contacto"})
        for field_name in ("nombreCompleto", "tipoDocumento", "numeroDocumento"):
            if not isinstance(contacto.get(field_name), str) or not contacto[field_name].strip():
                raise CheckoutError("DATO_INVALIDO", "Los datos de contacto son obligatorios", {"campo": f"contacto.{field_name}"})
        for field_name in ("telefono", "email"):
            if field_name in contacto and not isinstance(contacto[field_name], str):
                raise CheckoutError("DATO_INVALIDO", "El dato de contacto debe ser texto", {"campo": f"contacto.{field_name}"})
        pattern = DOCUMENT_PATTERNS.get(contacto["tipoDocumento"])
        if pattern is None or pattern.fullmatch(contacto["numeroDocumento"]) is None:
            raise CheckoutError("DATO_INVALIDO", "El documento no tiene un formato válido", {"campo": "contacto.numeroDocumento"})

        # SPEC-04 legacy flag, or the verified-contact guard (profile e-mail first).
        await self._ensure_verified_contact(input_data)

        # 1. Idempotencia real previa por clave
        existing = await _safe_call(self.checkouts, "obtener_por_idempotency_key", input_data.idempotency_key)
        if isinstance(getattr(existing, "customer_id", None), uuid.UUID):
            if existing.customer_id != input_data.customer_id:
                raise CheckoutError("CONFLICTO", "Idempotency-Key ya usada por otro usuario")
            return await self._resume_pending_sale(existing)

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
        pending_checkout: Any | None = None
        existing_cart_chk = await _safe_call(
            self.checkouts, "obtener_vivo_por_cart", cart_id, customer_id=input_data.customer_id
        )
        if isinstance(getattr(existing_cart_chk, "id", None), uuid.UUID):
            if existing_cart_chk.customer_id != input_data.customer_id:
                raise CheckoutError("RECURSO_NO_ENCONTRADO", "Checkout no encontrado")
            if existing_cart_chk.idempotency_key == input_data.idempotency_key:
                return await self._resume_pending_sale(existing_cart_chk)
            # A retry under a new key while a checkout is alive. A paid checkout is always
            # returned. A pending one is reused only if address, contact and cart content are
            # unchanged (decided once the lines are computed in step 5); otherwise it is
            # retired, because only one live checkout per cart can be stored.
            if existing_cart_chk.status != CheckoutStatus.PENDING_PAYMENT.value:
                return existing_cart_chk
            if datetime.now(timezone.utc) > existing_cart_chk.expires_at:
                await self._retire_checkout(
                    existing_cart_chk, "Checkout expirado por tiempo límite"
                )
            else:
                pending_checkout = existing_cart_chk

        # 4. Cálculo y revalidación de importes en el servidor
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
        # SPEC-10 Req. 2: revalidate every line before quoting or creating the order.
        await self._revalidate_stock(cart, lineas, items, subtotal)
        if pending_checkout is not None:
            if self._is_reusable(pending_checkout, input_data, lineas, subtotal):
                return await self._resume_pending_sale(pending_checkout)
            await self._retire_checkout(
                pending_checkout, "Checkout reemplazado por un nuevo intento"
            )
        quote = await self._quote_shipping(input_data, items)
        costo_envio = float(quote.costoEnvio)

        # The discount comes only from the cart snapshot, and only while that
        # snapshot is a complete, successful evaluation of the current lines;
        # otherwise the client must confirm the recalculated summary (no order
        # is created). A zero discount and an unavailable engine are different
        # states: `promociones_ok` tells them apart.
        descuento = await self._snapshot_discount(
            cart, items, subtotal, costo_envio
        )
        total_final = round(subtotal - descuento + costo_envio, 2)

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=15)
        # Local fallback id (Ventas replaces it); 10 hex chars so a burst of orders cannot
        # collide on the order_ref primary key.
        order_id = f"PED-{now.year}-{uuid.uuid4().hex[:10].upper()}"

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
            "avisoDireccion": await self._save_address(input_data),
        }

        sales_snapshot: dict[str, Any] | None = None
        if self.sales_client:
            from src.modules.mod_checkout.domain.snapshot_builder import SnapshotBuilder, SnapshotValidationError

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
                sales_snapshot = SnapshotBuilder.build_snapshot(
                    cliente_id=str(input_data.customer_id),
                    contacto=contacto_data,
                    items=snapshot_items,
                    envio=snapshot_envio,
                    pago=snapshot_pago,
                    cupon=cupon_data,
                )
            except SnapshotValidationError as exc:
                # The lines or amounts we computed cannot form a valid order: the cart must be
                # reviewed again, so reuse 409 CARRITO_DESACTUALIZADO with the server summary.
                logger.warning("Order snapshot rejected for cart %s: %s", cart_id, exc)
                raise CheckoutError(
                    "CARRITO_DESACTUALIZADO",
                    "No pudimos validar tu carrito. Revisa el resumen actualizado antes de continuar.",
                    {"summary": self._stale_summary(items, subtotal, costo_envio)},
                ) from exc
            summary[SALES_PENDING_KEY] = True
            # Persisted so the same-key retry replays the exact payload Ventas received.
            summary[SALES_SNAPSHOT_KEY] = sales_snapshot

        checkout_row = NewCheckout(
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

        # Another request won the unique-key/cart race: use its row instead of a duplicate.
        if getattr(saved, "id", None) != checkout_row.id:
            return await self._resume_pending_sale(saved)

        # Actualizar estado del carrito a IN_CHECKOUT
        await _safe_call(self.carts, "cambiar_estado", cart_id, "IN_CHECKOUT")

        if sales_snapshot is None:
            # Registrar referencia del pedido local (no hay pedido externo que reconciliar).
            if self.orders:
                try:
                    await _safe_call(
                        self.orders,
                        "guardar",
                        NewOrderReference(
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

        # Durable before the external call: a timeout leaves a checkoutId and a
        # provisional orderId the client can retry or query (issue #180).
        if self.uow:
            await _safe_call(self.uow, "commit")

        try:
            ventas_res = await self._create_order_in_sales(
                sales_snapshot, input_data.idempotency_key
            )
        except ModuleRequestError as exc:
            await self._retire_sales_pending(saved)
            raise self._sales_request_error(exc) from exc
        except ModuleUnavailableError as exc:
            raise self._sales_unavailable_error(saved, exc) from exc
        try:
            return await self._apply_sales_order(saved, ventas_res)
        except ModuleUnavailableError as exc:
            raise self._sales_unavailable_error(saved, exc) from exc

    async def _revalidate_stock(self, cart: Any, lineas: list[dict[str, Any]], items: list[Any], subtotal: float) -> None:
        """Block the checkout when a line is out of stock or cannot be confirmed (SPEC-10)."""
        if self.cart_validator is None:
            return
        try:
            validation = await self.cart_validator(cart)
        except Exception as exc:
            module, status, code = failure_summary(exc, default_module="products")
            logger.warning(
                "checkout inventory revalidation failed module=%s status=%s code=%s "
                "cart_id=%s correlation_id=%s",
                module,
                status,
                code,
                getattr(cart, "id", "?"),
                correlation_id_var.get(),
            )
            raise CheckoutError(
                "MODULO_NO_DISPONIBLE",
                "No pudimos confirmar el stock ahora. Intenta de nuevo en unos segundos.",
                {"modulo": getattr(exc, "module", "products")},
            ) from exc
        invalid: list[dict[str, Any]] = []
        for line in lineas:
            outcome = validation.get(line["sku"]) if isinstance(validation, dict) else None
            if isinstance(outcome, dict) and outcome.get("ok"):
                continue
            projection = str((outcome or {}).get("projection") or "SERVICIO_NO_DISPONIBLE")
            quantity = str((outcome or {}).get("quantity") or "SERVICIO_NO_DISPONIBLE")
            if projection == "SKU_AGOTADO":
                motivo, mensaje = "AGOTADO", f"{line['nombre']} se agotó mientras elegías."
            elif quantity == "STOCK_INSUFICIENTE":
                motivo, mensaje = "STOCK_INSUFICIENTE", f"No hay stock suficiente para {line['nombre']}."
            else:
                motivo, mensaje = "NO_CONFIRMADO", f"No pudimos confirmar el stock de {line['nombre']}."
            invalid.append(
                {
                    "sku": line["sku"],
                    "nombre": line["nombre"],
                    "cantidad": line["cantidad"],
                    "motivo": motivo,
                    "mensaje": mensaje,
                }
            )
        if invalid:
            raise CheckoutError(
                "CARRITO_DESACTUALIZADO",
                "Algunos productos de tu carrito cambiaron. Revisa tu carrito antes de continuar.",
                {
                    "lineas": invalid,
                    "summary": self._stale_summary(items, subtotal, 0.0),
                },
            )

    async def _save_address(self, input_data: CreateCheckoutInput) -> str | None:
        """SPEC-12 Req. 3: optional saved address; a Seguridad failure never blocks checkout."""
        if not input_data.guardar_direccion or not self.save_address_enabled:
            return None
        message = "No pudimos guardar tu dirección para la próxima vez."
        if self.security_client is None or not input_data.access_token:
            logger.warning("Address not saved: no holder token available")
            return message
        try:
            await self.security_client.save_address(
                str(input_data.customer_id),
                input_data.access_token,
                input_data.direccion or {},
            )
        except Exception as exc:
            logger.warning("Seguridad refused to save the address; checkout continues: %s", exc)
            return message
        return None

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
                correlation_id=correlation_id_var.get() or str(uuid.uuid4()),
            )
            quote = parse_quote(raw, request.distrito)
        except (ModuleUnavailableError, ModuleRequestError) as exc:
            module, status, code = failure_summary(exc, default_module="dispatch")
            logger.warning(
                "checkout dispatch quote failed module=%s status=%s code=%s correlation_id=%s",
                module,
                status,
                code,
                correlation_id_var.get(),
            )
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

    async def _snapshot_discount(
        self, cart: Any, items: list[Any], subtotal: float, costo_envio: float
    ) -> float:
        """Discount from the cart's last evaluation, or 409 when it cannot be trusted.

        A zero discount and an unavailable promotions engine are different
        states. The checkout only proceeds when the snapshot proves a successful
        evaluation of the current lines. On a stale snapshot it re-evaluates the
        cart once (the same path as `GET /carrito`) and persists the summary the
        customer must confirm, so the next attempt can succeed.
        """
        reason, descuento = self._snapshot_state(cart.totals_snapshot, subtotal)
        if reason is None:
            return descuento

        summary = self._stale_summary(items, subtotal, costo_envio)
        if self.cart_totals is not None:
            try:
                fresh = await self.cart_totals(cart)
            except Exception as exc:
                module, status, code = failure_summary(exc, default_module="products")
                logger.warning(
                    "checkout cart totals refresh failed module=%s status=%s code=%s "
                    "cart_id=%s correlation_id=%s",
                    module,
                    status,
                    code,
                    getattr(cart, "id", "?"),
                    correlation_id_var.get(),
                )
            else:
                if isinstance(fresh, dict) and fresh.get("promociones_ok") is True:
                    summary = self._summary_from_view(fresh, costo_envio)
        raise CheckoutError(
            "CARRITO_DESACTUALIZADO",
            self._snapshot_message(reason),
            {"motivo": reason, "summary": summary},
        )

    @staticmethod
    def _snapshot_state(snapshot: Any, subtotal: float) -> tuple[str | None, float]:
        """Return the reason a snapshot is untrustworthy (or None) and its discount."""
        if not isinstance(snapshot, dict) or snapshot.get("subtotal") is None:
            return "SIN_EVALUACION", 0.0
        if snapshot.get("promociones_ok") is not True:
            return "PROMOCIONES_NO_DISPONIBLES", 0.0
        try:
            snapshot_subtotal = Decimal(str(snapshot["subtotal"]))
        except (ArithmeticError, ValueError):
            return "SIN_EVALUACION", 0.0
        if snapshot_subtotal != Decimal(str(subtotal)):
            return "SUBTOTAL_CAMBIADO", 0.0
        discounts = snapshot.get("descuentos", [])
        if not isinstance(discounts, list):
            return "SIN_EVALUACION", 0.0
        try:
            descuento = sum(
                float(discount.get("amount", 0))
                for discount in discounts
                if isinstance(discount, dict)
            )
        except (ArithmeticError, TypeError, ValueError):
            return "SIN_EVALUACION", 0.0
        descuento = min(max(descuento, 0.0), subtotal)
        if snapshot.get("total") is not None:
            try:
                snapshot_total = Decimal(str(snapshot["total"]))
            except (ArithmeticError, ValueError):
                return "TOTAL_INCONSISTENTE", 0.0
            if snapshot_total != Decimal(str(subtotal)) - Decimal(str(descuento)):
                return "TOTAL_INCONSISTENTE", 0.0
        return None, descuento

    @staticmethod
    def _snapshot_message(reason: str) -> str:
        if reason == "PROMOCIONES_NO_DISPONIBLES":
            return (
                "No pudimos confirmar las promociones de tu carrito. "
                "Revisa el resumen e intenta de nuevo."
            )
        if reason == "TOTAL_INCONSISTENTE":
            return (
                "El total de tu carrito cambió. "
                "Revisa el resumen actualizado antes de continuar."
            )
        return "Tu carrito cambió. Revisa el resumen actualizado antes de continuar."

    @staticmethod
    def _summary_from_view(view: dict[str, Any], costo_envio: float) -> dict[str, Any]:
        """Summary for the 409 built from the just-recalculated cart view."""
        lineas = [
            {
                "sku": line.get("sku"),
                "cantidad": line.get("cantidad", 0),
                "precioUnitario": float(line.get("precio") or 0),
            }
            for line in view.get("lineas", [])
            if isinstance(line, dict) and line.get("disponible", True)
        ]
        subtotal = float(view.get("subtotal") or 0)
        descuento = sum(
            float(discount.get("monto") or 0)
            for discount in view.get("descuentos", [])
            if isinstance(discount, dict)
        )
        return {
            "lineas": lineas,
            "subtotal": round(subtotal, 2),
            "descuento": round(descuento, 2),
            "costoEnvio": round(costo_envio, 2),
            "total": round(subtotal - descuento + costo_envio, 2),
        }

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

    async def get_checkout(self, checkout_id: uuid.UUID, customer_id: uuid.UUID) -> Any:
        row = await self.checkouts.obtener_por_id(checkout_id)
        if not row or row.customer_id != customer_id:
            raise CheckoutError("RECURSO_NO_ENCONTRADO", "Checkout no encontrado")

        if row.status == CheckoutStatus.PENDING_PAYMENT.value and datetime.now(timezone.utc) > row.expires_at:
            if await self.expire_checkout(row.id):
                row.status = CheckoutStatus.EXPIRED.value

        return row

    @staticmethod
    def _mark_sales_resolved(row: Any, order_id: str) -> None:
        """Replace the provisional id and drop the reconciliation payload."""
        summary = dict(row.summary) if isinstance(row.summary, dict) else {}
        summary["orderId"] = str(order_id)
        summary.pop(SALES_PENDING_KEY, None)
        summary.pop(SALES_SNAPSHOT_KEY, None)
        row.summary = summary

    async def _order_id_for_cancellation(self, row: Any) -> str | None:
        """Real Sales order of a checkout, reconciling a timeout-left provisional one.

        A checkout persisted before the Sales call may expire before the order id is
        known. Replaying the same Idempotency-Key returns the order Ventas may have
        created, so the cancellation always targets the real `pedidoId`.
        """
        ord_row = await _safe_call(self.orders, "obtener_por_checkout_id", row.id)
        order_id = getattr(ord_row, "order_id", None) if ord_row else None
        if order_id:
            self._mark_sales_resolved(row, str(order_id))
            return str(order_id)

        summary = row.summary if isinstance(row.summary, dict) else {}
        if not self._is_sales_pending(row):
            return summary.get("orderId")

        snapshot = summary.get(SALES_SNAPSHOT_KEY)
        if not isinstance(snapshot, dict) or self.sales_client is None:
            logger.error(
                "Checkout %s expired with a pending Ventas order and no snapshot to reconcile",
                row.id,
            )
            return None
        try:
            ventas_res = await self._create_order_in_sales(snapshot, row.idempotency_key)
        except Exception as exc:
            logger.error(
                "Could not reconcile the pending Ventas order of checkout %s before expiry: %s",
                row.id,
                exc,
            )
            return None
        order_id = ventas_res.get("pedidoId") if isinstance(ventas_res, dict) else None
        if not order_id:
            logger.error("Ventas did not return a pedidoId while reconciling checkout %s", row.id)
            return None
        self._mark_sales_resolved(row, str(order_id))
        if self.orders:
            try:
                await _safe_call(
                    self.orders,
                    "guardar",
                    NewOrderReference(
                        order_id=str(order_id),
                        customer_id=row.customer_id,
                        checkout_id=row.id,
                        local_status="CREATED",
                        total=row.total,
                    ),
                )
            except Exception:
                pass
        return str(order_id)

    async def _apply_expiry(
        self,
        row: Any,
        *,
        comentario: str = "Checkout expirado por tiempo límite",
    ) -> None:
        """Single expiry routine (caller holds the row lock and commits).

        Marks the checkout EXPIRED, releases the cart, cancels the local order and always
        enqueues the Ventas cancellation so the stock reservation is not leaked.
        """
        await self.checkouts.marcar_estado(row.id, CheckoutStatus.EXPIRED.value)
        await _safe_call(self.carts, "cambiar_estado", row.cart_id, "ACTIVE")
        order_id = await self._order_id_for_cancellation(row)
        if not order_id:
            return
        if self.orders:
            await _safe_call(self.orders, "actualizar_estado", order_id, "CANCELLED")
        if self.outbox:
            await _safe_call(
                self.outbox,
                "guardar",
                NewOutboxTask(
                    type="REQUEST_CANCELLATION",
                    payload={
                        "order_id": order_id,
                        "motivo": "PAGO_NO_COMPLETADO",
                        "comentario": comentario,
                    },
                ),
            )

    async def _retire_checkout(self, checkout: Any, comentario: str) -> None:
        """Retire a live checkout that a new attempt replaces, cancelling its Sales order."""
        await self._apply_expiry(checkout, comentario=comentario)
        if self.uow:
            await _safe_call(self.uow, "commit")

    async def expire_checkout(self, checkout_id: uuid.UUID) -> bool:
        """Expire one checkout under its row lock; False when it is no longer due."""
        row = await self.checkouts.obtener_por_id(checkout_id, for_update=True)
        if (
            row is None
            or row.status != CheckoutStatus.PENDING_PAYMENT.value
            or datetime.now(timezone.utc) <= row.expires_at
        ):
            return False
        await self._apply_expiry(row)
        if self.uow:
            await _safe_call(self.uow, "commit")
        return True

    async def expire_due_checkouts(self, limit: int = 50) -> int:
        """Sweeper entry point (SPEC-14 `ExpirarCheckouts`): expires due rows, skipping locked ones."""
        due = await self.checkouts.listar_vencidos_para_expirar(datetime.now(timezone.utc), limit)
        for row in due:
            await self._apply_expiry(row)
        if due and self.uow:
            await _safe_call(self.uow, "commit")
        return len(due)

    async def _replay_existing_attempt(self, existing_attempt: Any, input_data: ProcessPaymentInput) -> Any | None:
        """Outcome of an already-processed Idempotency-Key, or None when the key is new."""
        if not isinstance(getattr(existing_attempt, "checkout_id", None), uuid.UUID):
            return None
        existing_checkout = await self.checkouts.obtener_por_id(existing_attempt.checkout_id)
        if not existing_checkout or existing_checkout.customer_id != input_data.customer_id:
            raise CheckoutError("RECURSO_NO_ENCONTRADO", "Checkout no encontrado")
        if existing_attempt.checkout_id != input_data.checkout_id:
            raise CheckoutError("CONFLICTO", "Idempotency-Key ya usada en otro checkout")
        if getattr(existing_attempt, "result", None) != PaymentResult.APPROVED.value:
            # Replay of a declined/errored attempt must report the original outcome, never success.
            raise self._replayed_failure(existing_attempt, existing_checkout)
        return existing_attempt

    async def process_payment(self, input_data: ProcessPaymentInput) -> Any:
        if not input_data.idempotency_key:
            raise CheckoutError("VALIDACION", "Se requiere Idempotency-Key", {"campo": "Idempotency-Key"})

        # 1. Idempotencia real: buscar por clave antes de ejecutar
        existing_attempt = await _safe_call(self.payment_attempts, "obtener_por_idempotency_key", input_data.idempotency_key)
        replay = await self._replay_existing_attempt(existing_attempt, input_data)
        if replay is not None:
            return replay

        # Row lock for the rest of the transaction: concurrent payments of one checkout serialize here.
        row = await self.checkouts.obtener_por_id(input_data.checkout_id, for_update=True)
        if not row or row.customer_id != input_data.customer_id:
            raise CheckoutError("RECURSO_NO_ENCONTRADO", "Checkout no encontrado")

        # A request with the same key may have won the lock first: re-check under the lock.
        existing_attempt = await _safe_call(self.payment_attempts, "obtener_por_idempotency_key", input_data.idempotency_key)
        replay = await self._replay_existing_attempt(existing_attempt, input_data)
        if replay is not None:
            return replay

        now = datetime.now(timezone.utc)
        if row.status == CheckoutStatus.EXPIRED.value or (
            row.status == CheckoutStatus.PENDING_PAYMENT.value and now > row.expires_at
        ):
            if row.status != CheckoutStatus.EXPIRED.value:
                await self._apply_expiry(row)
                if self.uow:
                    await _safe_call(self.uow, "commit")
            raise CheckoutError("CHECKOUT_EXPIRADO", "El checkout venció hace más de 15 minutos")

        if self._is_sales_pending(row):
            # Never charge for a checkout whose Sales order is still provisional: the
            # client must reconcile with the same Idempotency-Key first.
            summary = row.summary if isinstance(row.summary, dict) else {}
            raise CheckoutError(
                "MODULO_NO_DISPONIBLE",
                "Todavía estamos confirmando tu pedido con Ventas. "
                "Reintenta el checkout con la misma Idempotency-Key.",
                {
                    "modulo": "sales",
                    "motivo": "PEDIDO_EN_PROCESO",
                    "checkoutId": str(row.id),
                    "orderId": summary.get("orderId"),
                    "orderIdProvisional": True,
                    "reintentable": True,
                },
            )

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

            attempt = NewPaymentAttempt(
                checkout_id=row.id,
                idempotency_key=input_data.idempotency_key,
                result=PaymentResult.APPROVED.value,
                brand=payment_res.brand,
                last4=payment_res.last4,
                amount=row.total,
                transaction_id=payment_res.transaction_id,
            )
            try:
                saved_attempt = await self.payment_attempts.guardar(attempt)
            except PaymentConflictError as exc:
                raise CheckoutError("CONFLICTO", "El checkout ya tiene un pago aprobado") from exc

            # Encolar notificación a Ventas en outbox (SPEC-15 Req. 2)
            if order_id and self.outbox:
                payment_payload = {
                    "transaccionId": payment_res.transaction_id or f"SIM-{uuid.uuid4().hex[:8].upper()}",
                    "resultado": "APROBADO",
                    "monto": float(row.total),
                    "moneda": "PEN",
                    "metodo": "TARJETA_CREDITO",
                    "marcaTarjeta": payment_res.brand or "VISA",
                    "fechaPago": datetime.now(timezone.utc).isoformat(),
                    "codigoAutorizacion": f"AUTH-{uuid.uuid4().hex[:6].upper()}",
                }
                # G5 declares `ultimosCuatroDigitos` with pattern ^\d{4}$ and
                # does not accept null/placeholders: omit it when unknown.
                if payment_res.last4:
                    payment_payload["ultimosCuatroDigitos"] = payment_res.last4
                outbox_entry = NewOutboxTask(
                    type="NOTIFY_SALES_PAYMENT",
                    payload={
                        "order_id": order_id,
                        "payment_data": payment_payload,
                    },
                )
                await _safe_call(self.outbox, "guardar", outbox_entry)

            # The order is NOT confirmed here (decision "Pendiente hasta G5"): it waits in
            # PAYMENT_PENDING_NOTIFICATION until the outbox gets Ventas' answer, which sets
            # PAID_NOTIFIED and enqueues the confirmation e-mail, or CANCELLED (late payment).
            if order_id and ord_row and getattr(ord_row, "order_id", None):
                await _safe_call(self.orders, "actualizar_estado", order_id, "PAYMENT_PENDING_NOTIFICATION")

            if self.uow:
                await _safe_call(self.uow, "commit")
            return saved_attempt

        # 5. Caso rechazado o error
        updated = await self.checkouts.incrementar_intentos(row.id)
        if updated is None:
            raise CheckoutError(
                "PAGO_RECHAZADO",
                "Se agotaron los intentos de pago permitidos",
                {"motivo": "INTENTOS_AGOTADOS", "intentosRestantes": 0},
            )
        counted = getattr(updated, "payment_attempts", None)
        current_attempts = counted if isinstance(counted, int) else (row.payment_attempts or 0) + 1
        remaining = max(0, 3 - current_attempts)

        if current_attempts >= 3:
            await self.checkouts.marcar_estado(row.id, CheckoutStatus.FAILED.value)
            await _safe_call(self.carts, "cambiar_estado", row.cart_id, "ACTIVE")
            ord_row = await _safe_call(self.orders, "obtener_por_checkout_id", row.id)
            if ord_row and getattr(ord_row, "order_id", None):
                await _safe_call(self.orders, "actualizar_estado", ord_row.order_id, "CANCELLED")
                if self.outbox:
                    outbox_entry = NewOutboxTask(
                        type="REQUEST_CANCELLATION",
                        payload={
                            "order_id": ord_row.order_id,
                            "motivo": "PAGO_NO_COMPLETADO",
                            "comentario": "Intentos de pago con tarjeta agotados (3 rechazos)",
                        },
                    )
                    await _safe_call(self.outbox, "guardar", outbox_entry)

        attempt = NewPaymentAttempt(
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

        raise self._failure_error(payment_res.result, payment_res.reason, remaining)

    @staticmethod
    def _failure_error(result: str | None, reason: str | None, remaining: int) -> CheckoutError:
        motivo_map = {
            PaymentRejectReason.INSUFFICIENT_FUNDS.value: "FONDOS_INSUFICIENTES",
            PaymentRejectReason.DECLINED_BY_ISSUER.value: "RECHAZADO_POR_EMISOR",
            PaymentRejectReason.PROCESSING_ERROR.value: "ERROR_PROCESAMIENTO",
        }
        motivo_es = motivo_map.get(reason or "", reason or "RECHAZADO")

        if result == PaymentResult.ERROR.value:
            return CheckoutError(
                "ERROR_PROCESAMIENTO",
                "Ocurrió un error temporal al procesar el pago. Intente de nuevo.",
                {"motivo": motivo_es, "intentosRestantes": remaining},
            )

        return CheckoutError(
            "PAGO_RECHAZADO",
            "Tu tarjeta fue rechazada. Puedes intentar con otra tarjeta.",
            {"motivo": motivo_es, "intentosRestantes": remaining},
        )

    @classmethod
    def _replayed_failure(cls, attempt: Any, checkout: Any) -> CheckoutError:
        remaining = max(0, 3 - (getattr(checkout, "payment_attempts", 0) or 0))
        return cls._failure_error(getattr(attempt, "result", None), getattr(attempt, "reason", None), remaining)
