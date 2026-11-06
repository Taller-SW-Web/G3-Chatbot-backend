"""Snapshot builder for Ventas order recording (SPEC-15 Req. 1, 4).

Validates snapshot math and constructs the exact shape expected by Ventas:
subtotal - descuentoCupon + costoEnvio == total (tolerance 0.01).
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

TWO_PLACES = Decimal("0.01")


def _to_decimal(val: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(val)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field_name} debe ser un numero valido, se recibio: {val!r}") from exc


class SnapshotValidationError(ValueError):
    """Raised when the snapshot items or amounts fail validation."""
    pass


class SnapshotBuilder:
    @staticmethod
    def build_snapshot(
        *,
        cliente_id: str,
        contacto: dict[str, Any],
        items: list[dict[str, Any]],
        cupon: dict[str, Any] | None = None,
        envio: dict[str, Any],
        pago: dict[str, Any],
        canal: str = "CHATBOT",
    ) -> dict[str, Any]:
        if not items:
            raise SnapshotValidationError("El pedido debe contener al menos un item.")

        # Excluir lineas no disponibles (SPEC-15 Req. 4)
        active_items = []
        for it in items:
            if it.get("disponible", True) is False or it.get("no_disponible", False) is True:
                continue
            active_items.append(it)

        if not active_items:
            raise SnapshotValidationError("No hay items disponibles para procesar el pedido.")

        # Validar items
        formatted_items = []
        for it in active_items:
            prod_id = str(it.get("productoId") or it.get("product_id") or it.get("id") or "").strip()
            sku = str(it.get("sku") or "").strip()
            desc = str(it.get("descripcion") or it.get("description") or it.get("nombre") or "").strip()
            qty = int(it.get("cantidad") or it.get("quantity") or 1)
            price = _to_decimal(it.get("precioUnitario") or it.get("precio") or it.get("unit_price") or 0, "precioUnitario")
            if not sku:
                raise SnapshotValidationError("Cada item debe tener un SKU valido.")
            if qty <= 0:
                raise SnapshotValidationError(f"Cantidad invalida para el item {sku}: {qty}")

            formatted_items.append({
                "productoId": prod_id,
                "sku": sku,
                "descripcion": desc,
                "cantidad": qty,
                "precioUnitario": float(price),
            })

        # Cupon
        cupon_data = {
            "codigo": str((cupon or {}).get("codigo") or (cupon or {}).get("code") or ""),
            "descuento": float(_to_decimal((cupon or {}).get("descuento") or (cupon or {}).get("discount") or 0, "descuentoCupon")),
            "aplicado": bool((cupon or {}).get("aplicado", False) if cupon else False),
        }

        # Envio
        envio_data = {
            "modalidad": str(envio.get("modalidad") or "DELIVERY"),
            "costo": float(_to_decimal(envio.get("costo") or envio.get("costoEnvio") or 0, "costoEnvio")),
            "destinatario": str(envio.get("destinatario") or contacto.get("nombreCompleto") or "").strip(),
            "departamento": str(envio.get("departamento") or "").strip(),
            "provincia": str(envio.get("provincia") or "").strip(),
            "distrito": str(envio.get("distrito") or "").strip(),
            "direccion": str(envio.get("direccion") or "").strip(),
            "referencia": str(envio.get("referencia") or "").strip(),
        }

        # Pago y coherencia matematica (SPEC-15 Req. 4)
        subtotal_dec = _to_decimal(pago.get("subtotal") or 0, "subtotal")
        descuento_dec = _to_decimal(cupon_data["descuento"], "descuentoCupon")
        costo_envio_dec = _to_decimal(envio_data["costo"], "costoEnvio")
        total_dec = _to_decimal(pago.get("total") or 0, "total")

        expected_total = subtotal_dec - descuento_dec + costo_envio_dec
        if abs(expected_total - total_dec) > Decimal("0.01"):
            raise SnapshotValidationError(
                f"Incoherencia en totales: subtotal({subtotal_dec}) - descuento({descuento_dec}) + envio({costo_envio_dec}) != total({total_dec})"
            )

        pago_data = {
            "metodoPago": str(pago.get("metodoPago") or "TARJETA_CREDITO"),
            "moneda": str(pago.get("moneda") or "PEN"),
            "subtotal": float(subtotal_dec),
            "descuentoCupon": float(descuento_dec),
            "costoEnvio": float(costo_envio_dec),
            "total": float(total_dec),
        }

        contacto_data = {
            "clienteId": str(cliente_id),
            "nombreCompleto": str(contacto.get("nombreCompleto") or "").strip(),
            "tipoDocumento": str(contacto.get("tipoDocumento") or "DNI").strip(),
            "numeroDocumento": str(contacto.get("numeroDocumento") or "").strip(),
            "telefono": str(contacto.get("telefono") or "").strip(),
            "email": str(contacto.get("email") or "").strip(),
        }

        return {
            "canal": canal,
            "contacto": contacto_data,
            "items": formatted_items,
            "cupon": cupon_data,
            "envio": envio_data,
            "pago": pago_data,
        }
