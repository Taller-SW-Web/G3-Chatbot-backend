"""Convención hito-2.md: todos los modelos se importan aquí para que Alembic autogenerate los detecte vía Base.metadata."""
from src.infrastructure.db.connection import Base

try:
    from src.adapters.outbound.persistence.models.celular_verificacion import CelularVerificacionLocal
    __all__ = ["Base", "CelularVerificacionLocal"]
except ImportError:
    # H2-01: base sin modelos aún; H2-03..H2-07 agregarán aquí sus imports.
    __all__ = ["Base"]

from src.adapters.outbound.persistence.models.evidencia import Evidencia
from src.adapters.outbound.persistence.models.mensaje import Mensaje

__all__ += ["Mensaje", "Evidencia"]

# H2-03..H2-07 agregarán aquí: conversacion, carrito, item_carrito,
# checkout, intento_pago, pedido_ref, notificacion, reclamo_ref, devolucion_ref,
# outbox, adjunto (SPEC-23). Hoy esos ficheros existen pero están vacíos.
