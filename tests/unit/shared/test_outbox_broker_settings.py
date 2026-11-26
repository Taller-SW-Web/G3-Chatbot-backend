"""Settings of the RabbitMQ relay (ADR-0030): mode, URL and prefetch."""
import pytest

from src.shared.infrastructure.config.settings import Settings, _build_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("OUTBOX_DISPATCH_MODE", "RABBITMQ_URL", "RABBITMQ_PREFETCH"):
        monkeypatch.delenv(key, raising=False)


def test_defaults_keep_the_direct_dispatch():
    settings = _build_settings()
    assert settings.outbox_dispatch_mode == "direct"
    assert settings.rabbitmq_url == ""
    assert settings.rabbitmq_prefetch == 10


def test_direct_mode_does_not_need_the_url():
    assert Settings(outbox_dispatch_mode="direct").rabbitmq_url == ""


def test_broker_mode_reads_the_environment(monkeypatch):
    monkeypatch.setenv("OUTBOX_DISPATCH_MODE", " Broker ")
    monkeypatch.setenv("RABBITMQ_URL", "amqps://user:pw@host.example.com/vhost")
    monkeypatch.setenv("RABBITMQ_PREFETCH", "25")
    settings = _build_settings()
    assert settings.outbox_dispatch_mode == "broker"
    assert settings.rabbitmq_url == "amqps://user:pw@host.example.com/vhost"
    assert settings.rabbitmq_prefetch == 25


def test_broker_mode_without_url_fails_at_startup(monkeypatch):
    monkeypatch.setenv("OUTBOX_DISPATCH_MODE", "broker")
    with pytest.raises(ValueError, match="requires RABBITMQ_URL"):
        _build_settings()


def test_unknown_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("OUTBOX_DISPATCH_MODE", "queue")
    with pytest.raises(ValueError, match="OUTBOX_DISPATCH_MODE"):
        _build_settings()


@pytest.mark.parametrize("url", ["http://host", "host:5672", "rabbit"])
def test_broker_mode_requires_an_amqp_scheme(url):
    with pytest.raises(ValueError, match="amqp"):
        Settings(outbox_dispatch_mode="broker", rabbitmq_url=url)


@pytest.mark.parametrize("raw", ["0", "-3"])
def test_prefetch_must_be_positive(monkeypatch, raw):
    monkeypatch.setenv("RABBITMQ_PREFETCH", raw)
    with pytest.raises(ValueError, match="RABBITMQ_PREFETCH"):
        _build_settings()


def test_prefetch_must_be_a_number(monkeypatch):
    monkeypatch.setenv("RABBITMQ_PREFETCH", "many")
    with pytest.raises(ValueError, match="RABBITMQ_PREFETCH"):
        _build_settings()


def test_the_url_never_appears_in_the_settings_repr():
    settings = Settings(outbox_dispatch_mode="broker", rabbitmq_url="amqps://user:s3cret@host/vh")
    assert "s3cret" not in repr(settings)
