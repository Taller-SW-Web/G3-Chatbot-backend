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

from src.adapters.outbound.persistence.models.evidence import Evidence
from src.adapters.outbound.persistence.models.message import Message

__all__ += ["Message", "Evidence"]

# H2-03/H2-04 agregarán aquí: cart, cart_item, checkout, payment_attempt,
# order_ref, notification, claim_ref, return_ref y outbox.
