"""SMTP e-mail sender (SPEC-16). Mailtrap in development, any SMTP provider in production.

Credentials come only from the environment (SMTP_HOST, SMTP_PORT, SMTP_USER,
SMTP_PASSWORD, SMTP_FROM, SMTP_STARTTLS). smtplib is blocking, so each send
runs in a worker thread. Every provider failure is raised as
`EmailDeliveryError` so the notification service can schedule a retry.
"""
import asyncio
import smtplib
import ssl
from email.message import EmailMessage as MimeMessage

from src.ports.outbound.email_sender_port import EmailDeliveryError, EmailMessage


class SmtpEmailSender:
    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        *,
        username: str = "",
        password: str = "",
        starttls: bool = True,
        timeout_seconds: float = 10.0,
    ):
        self.host = host
        self.port = port
        self.sender = sender
        self._username = username
        self._password = password
        self.starttls = starttls
        self.timeout_seconds = timeout_seconds

    async def send(self, message: EmailMessage) -> None:
        await asyncio.to_thread(self._send_blocking, self._mime(message))

    def _mime(self, message: EmailMessage) -> MimeMessage:
        mime = MimeMessage()
        mime["From"] = self.sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")
        return mime

    def _send_blocking(self, mime: MimeMessage) -> None:
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds) as smtp:
                if self.starttls:
                    # Explicito: el certificado del servidor SMTP se verifica siempre.
                    context = ssl.create_default_context()
                    context.check_hostname = True
                    context.verify_mode = ssl.CERT_REQUIRED
                    smtp.starttls(context=context)
                if self._username:
                    smtp.login(self._username, self._password)
                smtp.send_message(mime)
        except (smtplib.SMTPException, OSError) as exc:
            # Never include the password; the message is kept as last_error.
            raise EmailDeliveryError(f"SMTP {self.host}:{self.port} failed: {type(exc).__name__}: {exc}") from exc
