"""Outbound ports: e-mail delivery and template rendering (SPEC-16).

Provider-neutral: no smtplib or Jinja2 imports here. `EmailDeliveryError`
marks a failure worth retrying (provider down, timeout, rejected login);
the notification service schedules the retries (1, 5 and 15 min).
"""
from dataclasses import dataclass
from typing import Any, Protocol


class EmailDeliveryError(Exception):
    """The provider did not accept the message; the send can be retried."""


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    html: str
    text: str


class EmailSenderPort(Protocol):
    async def send(self, message: EmailMessage) -> None:
        """Deliver one message or raise `EmailDeliveryError`."""
        ...


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str


class EmailTemplatePort(Protocol):
    def render_order_confirmation(self, context: dict[str, Any]) -> RenderedEmail:
        """HTML and plain-text versions of the order confirmation (SPEC-16 Req. 1)."""
        ...
