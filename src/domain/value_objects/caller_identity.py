"""Who is calling: an authenticated customer or an anonymous visitor (SPEC-05 Req. 1)."""
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class CallerIdentity:
    """Exactly one of the two fields is set.

    `anonymous_sid` is the stored (hashed) value of the `chat_sid` cookie, never the raw cookie.
    """

    customer_id: uuid.UUID | None = None
    anonymous_sid: str | None = None

    @property
    def customer(self) -> uuid.UUID:
        """`customer_id` de un llamante autenticado (lo garantiza `require_customer`)."""
        if self.customer_id is None:
            raise ValueError("anonymous caller has no customer_id")
        return self.customer_id

    def __post_init__(self) -> None:
        if (self.customer_id is None) == (self.anonymous_sid is None):
            raise ValueError("CallerIdentity needs exactly one of customer_id or anonymous_sid")
