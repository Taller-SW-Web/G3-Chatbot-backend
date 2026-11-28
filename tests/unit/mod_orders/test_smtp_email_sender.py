"""SmtpEmailSender and e-mail wiring (SPEC-16), without a network.

Trazabilidad: SPEC-16 RNF Seguridad (credenciales SMTP por variables de entorno)
y Req. 2 (un fallo del proveedor se reintenta).
"""
import smtplib

import pytest

from src.modules.mod_orders import module as orders_module
from src.modules.mod_orders.adapters import smtp_email_adapter
from src.modules.mod_orders.adapters.fake_email_sender import FakeEmailSender
from src.modules.mod_orders.adapters.smtp_email_adapter import SmtpEmailSender
from src.modules.mod_orders.ports.email_sender_port import EmailDeliveryError, EmailMessage
from src.shared.infrastructure.config.settings import Settings

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
    assert isinstance(orders_module.provide_email_sender(Settings()), FakeEmailSender)


def test_con_smtp_host_se_usa_smtp():
    sender = orders_module.provide_email_sender(Settings(smtp_host="sandbox.smtp.mailtrap.io", smtp_port=2525))
    assert isinstance(sender, SmtpEmailSender)
    assert (sender.host, sender.port) == ("sandbox.smtp.mailtrap.io", 2525)


def test_email_mode_fake_wins_over_a_configured_host():
    settings = Settings(email_mode="fake", smtp_host="sandbox.smtp.mailtrap.io")

    assert isinstance(orders_module.provide_email_sender(settings), FakeEmailSender)


def test_email_mode_smtp_without_host_is_refused():
    with pytest.raises(ValueError, match="SMTP_HOST"):
        Settings(email_mode="smtp")
