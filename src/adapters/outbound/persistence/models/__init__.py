"""Convención hito-2.md: todos los modelos se importan aquí para que Alembic autogenerate los detecte vía Base.metadata."""
from src.infrastructure.db.connection import Base

try:
    from src.adapters.outbound.persistence.models.local_phone_verification import LocalPhoneVerification
    from src.adapters.outbound.persistence.models.conversation import Conversation
    from src.adapters.outbound.persistence.models.attachment import Attachment
    __all__ = ["Base", "LocalPhoneVerification", "Conversation", "Attachment"]
except ImportError:
    # H2-01: base sin modelos aún; H2-03..H2-07 agregarán aquí sus imports.
    __all__ = ["Base"]

# El esquema tiene 14 tablas con nombres en inglés. Ya registradas:
# local_phone_verification (H2-02), conversation y attachment (H2-07; attachment es la 14ª).
# H2-03..H2-07 agregarán aquí las 11 restantes: message, cart, cart_item,
# checkout, payment_attempt, order_ref, notification, claim_ref, return_ref,
# evidence, outbox.
# Hoy sus ficheros de modelo existen pero están vacíos (conservan los nombres
# antiguos en español: mensaje.py, carrito.py, item_carrito.py, etc.).
