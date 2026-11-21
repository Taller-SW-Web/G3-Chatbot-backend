"""Production dependency wiring for SPEC-05 sensitive-data redaction."""

from types import SimpleNamespace

from src.modules.mod_chat import module as chat_module


def test_interpret_and_respond_wires_sensitive_data_filter(monkeypatch):
    settings = SimpleNamespace(assistant_name="Botleta", llm_timeout_seconds=15)
    monkeypatch.setattr(chat_module, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_module, "ConversationPostgresAdapter", lambda _: object())
    monkeypatch.setattr(chat_module, "MessagePostgresAdapter", lambda _: object())
    monkeypatch.setattr(chat_module, "SqlAlchemyUnitOfWork", lambda _: object())
    monkeypatch.setattr(chat_module, "provide_llm_provider", lambda _: object())
    monkeypatch.setattr(chat_module, "provide_tool_executor", lambda _: object())

    use_case = chat_module.build_interpret_and_respond(object())

    assert use_case._sanitize("mi contraseña es Secreta123!") == "mi contraseña es [contraseña oculta]!"
