"""SmtpEmailSender and e-mail wiring (SPEC-16), without a network.

Trazabilidad: SPEC-16 RNF Seguridad (credenciales SMTP por variables de entorno)
y Req. 2 (un fallo del proveedor se reintenta).
"""
import smtplib

import pytest

from src.adapters.outbound.fakes.fake_email_sender import FakeEmailSender
from src.adapters.outbound.notificaciones import smtp_email_adapter
from src.adapters.outbound.notificaciones.smtp_email_adapter import SmtpEmailSender
from src.infrastructure.config import container
from src.infrastructure.config.settings import Settings
from src.ports.outbound.email_sender_port import EmailDeliveryError, EmailMessage

MESSAGE = EmailMessage(to="maria@ejemplo.com", subject="Confirmamos tu pedido PED-1", html="<p>hola</p>", text="hola")


class RecordingSMTP:
    instances: list["RecordingSMTP"] = []
    fail_on_send = False

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent = None
        RecordingSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(f"login:{user}")

    def send_message(self, mime):
        if RecordingSMTP.fail_on_send:
            raise smtplib.SMTPServerDisconnected("connection closed")
        self.sent = mime


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    RecordingSMTP.instances = []
    RecordingSMTP.fail_on_send = False
    monkeypatch.setattr(smtp_email_adapter.smtplib, "SMTP", RecordingSMTP)


@pytest.mark.asyncio
async def test_envia_multipart_con_starttls_y_login():
    sender = SmtpEmailSender("sandbox.smtp.mailtrap.io", 2525, "Botleta <no-reply@x.pe>",
                             username="user", password="secret")

    await sender.send(MESSAGE)

    smtp = RecordingSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("sandbox.smtp.mailtrap.io", 2525)
    assert smtp.calls == ["starttls", "login:user"]
    assert smtp.sent["To"] == "maria@ejemplo.com"
    assert smtp.sent["Subject"] == "Confirmamos tu pedido PED-1"
    assert [part.get_content_type() for part in smtp.sent.iter_parts()] == ["text/plain", "text/html"]


@pytest.mark.asyncio
async def test_fallo_del_proveedor_es_reintentable_y_no_expone_la_clave():
    RecordingSMTP.fail_on_send = True
    sender = SmtpEmailSender("smtp.x.pe", 587, "no-reply@x.pe", username="user", password="secret")

    with pytest.raises(EmailDeliveryError) as exc:
        await sender.send(MESSAGE)

    assert "secret" not in str(exc.value)


def test_sin_smtp_host_se_usa_el_sender_falso():
    assert isinstance(container.provide_email_sender(Settings()), FakeEmailSender)


def test_con_smtp_host_se_usa_smtp():
    sender = container.provide_email_sender(Settings(smtp_host="sandbox.smtp.mailtrap.io", smtp_port=2525))
    assert isinstance(sender, SmtpEmailSender)
    assert (sender.host, sender.port) == ("sandbox.smtp.mailtrap.io", 2525)
