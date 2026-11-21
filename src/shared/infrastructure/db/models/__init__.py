"""Convención: todos los modelos se importan aquí para que Alembic autogenerate los detecte vía Base.metadata."""
from src.shared.infrastructure.db.connection import Base

try:
    from src.shared.infrastructure.db.models.attachment import Attachment
    from src.shared.infrastructure.db.models.conversation import Conversation
    from src.shared.infrastructure.db.models.local_phone_verification import LocalPhoneVerification
    __all__ = ["Base", "LocalPhoneVerification", "Conversation", "Attachment"]
except ImportError:
    # Base sin modelos aún; los modelos agregan aquí sus imports.
    __all__ = ["Base"]

from src.shared.infrastructure.db.models.evidence import Evidence
from src.shared.infrastructure.db.models.message import Message

__all__ += ["Message", "Evidence"]

# cart, cart_item, order_ref y outbox (SPEC-10, 11, 15).
from src.shared.infrastructure.db.models.cart import Cart
from src.shared.infrastructure.db.models.cart_item import CartItem
from src.shared.infrastructure.db.models.order_ref import OrderRef
from src.shared.infrastructure.db.models.outbox import Outbox

__all__ += ["Cart", "CartItem", "OrderRef", "Outbox"]

# checkout, payment_attempt, notification, claim_ref y
# return_ref (SPEC-12, 14, 16, 19 a 22).
from src.shared.infrastructure.db.models.checkout import Checkout
from src.shared.infrastructure.db.models.claim_ref import ClaimRef
from src.shared.infrastructure.db.models.notification import Notification
from src.shared.infrastructure.db.models.payment_attempt import PaymentAttempt
from src.shared.infrastructure.db.models.return_ref import ReturnRef

__all__ += ["Checkout", "PaymentAttempt", "Notification", "ClaimRef", "ReturnRef"]
