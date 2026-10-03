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

# H2-03 (Sonny): cart, cart_item, order_ref y outbox (SPEC-10, 11, 15).
from src.adapters.outbound.persistence.models.cart import Cart
from src.adapters.outbound.persistence.models.cart_item import CartItem
from src.adapters.outbound.persistence.models.order_ref import OrderRef
from src.adapters.outbound.persistence.models.outbox import Outbox

__all__ += ["Cart", "CartItem", "OrderRef", "Outbox"]

# H2-04 (David): checkout, payment_attempt, notification, claim_ref y
# return_ref (SPEC-12, 14, 16, 19 a 22).
from src.adapters.outbound.persistence.models.checkout import Checkout
from src.adapters.outbound.persistence.models.payment_attempt import PaymentAttempt
from src.adapters.outbound.persistence.models.notification import Notification
from src.adapters.outbound.persistence.models.claim_ref import ClaimRef
from src.adapters.outbound.persistence.models.return_ref import ReturnRef

__all__ += ["Checkout", "PaymentAttempt", "Notification", "ClaimRef", "ReturnRef"]
