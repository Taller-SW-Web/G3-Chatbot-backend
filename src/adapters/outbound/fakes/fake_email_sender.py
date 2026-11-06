"""In-memory e-mail sender (SPEC-16): tests, and local runs without SMTP_HOST.

Keeps every accepted message in `sent` and logs its subject. `fail_times`
makes the next N sends raise `EmailDeliveryError`, to exercise the retries.
"""
import logging

from src.ports.outbound.email_sender_port import EmailDeliveryError, EmailMessage

logger = logging.getLogger(__name__)


class FakeEmailSender:
    def __init__(self, fail_times: int = 0):
        self.sent: list[EmailMessage] = []
        self.calls = 0
        self.fail_times = fail_times

    async def send(self, message: EmailMessage) -> None:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise EmailDeliveryError("fake SMTP unavailable")
        self.sent.append(message)
        logger.info("FakeEmailSender: %r to %s (not delivered)", message.subject, message.to)
