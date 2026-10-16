"""Production dependency wiring for SPEC-05 sensitive-data redaction."""

from types import SimpleNamespace

from src.infrastructure.config import container


def test_interpret_and_respond_wires_sensitive_data_filter(monkeypatch):
    settings = SimpleNamespace(assistant_name="Botleta", llm_timeout_seconds=15)
    monkeypatch.setattr(container, "get_settings", lambda: settings)
    monkeypatch.setattr(container, "ConversationPostgresAdapter", lambda _: object())
    monkeypatch.setattr(container, "MessagePostgresAdapter", lambda _: object())
    monkeypatch.setattr(container, "SqlAlchemyUnitOfWork", lambda _: object())
    monkeypatch.setattr(container, "provide_llm_provider", lambda _: object())
    monkeypatch.setattr(container, "provide_tool_executor", lambda _: object())

    use_case = container.build_interpret_and_respond(object())

    assert use_case._sanitize("mi contraseña es Secreta123!") == "mi contraseña es [contraseña oculta]!"
