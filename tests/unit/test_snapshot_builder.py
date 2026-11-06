import pytest
from src.domain.services.snapshot_builder import SnapshotBuilder, SnapshotValidationError


def test_snapshot_builder_success():
    contacto = {
        "nombreCompleto": "Juan Perez",
        "tipoDocumento": "DNI",
        "numeroDocumento": "72458912",
        "telefono": "+51999888777",
        "email": "juan@example.com",
    }
    items = [
        {"productoId": "P-1", "sku": "SKU-1", "descripcion": "Zapatilla", "cantidad": 1, "precioUnitario": 100.0}
    ]
    cupon = {"codigo": "PROMO10", "descuento": 10.0, "aplicado": True}
    envio = {"modalidad": "DELIVERY", "costo": 15.0, "distrito": "Miraflores"}
    pago = {"subtotal": 100.0, "total": 105.0}

    snapshot = SnapshotBuilder.build_snapshot(
        cliente_id="user-123",
        contacto=contacto,
        items=items,
        cupon=cupon,
        envio=envio,
        pago=pago,
    )
    assert snapshot["canal"] == "CHATBOT"
    assert snapshot["contacto"]["clienteId"] == "user-123"
    assert snapshot["items"][0]["sku"] == "SKU-1"
    assert snapshot["pago"]["total"] == 105.0


def test_snapshot_builder_filters_unavailable_items():
    contacto = {"nombreCompleto": "Juan"}
    items = [
        {"sku": "SKU-DISP", "cantidad": 1, "precioUnitario": 50.0, "disponible": True},
        {"sku": "SKU-NO-DISP", "cantidad": 1, "precioUnitario": 50.0, "disponible": False},
    ]
    envio = {"costo": 10.0}
    pago = {"subtotal": 50.0, "total": 60.0}

    snapshot = SnapshotBuilder.build_snapshot(
        cliente_id="user-1",
        contacto=contacto,
        items=items,
        envio=envio,
        pago=pago,
    )
    assert len(snapshot["items"]) == 1
    assert snapshot["items"][0]["sku"] == "SKU-DISP"


def test_snapshot_builder_raises_on_incoherent_total():
    contacto = {"nombreCompleto": "Juan"}
    items = [{"sku": "SKU-1", "cantidad": 1, "precioUnitario": 100.0}]
    envio = {"costo": 10.0}
    pago = {"subtotal": 100.0, "total": 999.0}

    with pytest.raises(SnapshotValidationError):
        SnapshotBuilder.build_snapshot(
            cliente_id="user-1",
            contacto=contacto,
            items=items,
            envio=envio,
            pago=pago,
        )
