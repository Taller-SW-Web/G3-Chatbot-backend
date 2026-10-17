"""Outbound port: read the latest document of a customer's checkout (SPEC-03 profile).

`ultimoDocumento` is a masked projection of `summary.contacto` of the most
recent channel checkout (modelo-datos.md §AccountPage); the full number never
leaves the order snapshot.
"""
import uuid
from typing import Protocol


class CheckoutDocumentPort(Protocol):
    async def latest_document(self, customer_id: uuid.UUID) -> dict[str, str] | None:
        """`{"tipo", "numero"}` of the most recent checkout, or None when there is none."""
        ...
