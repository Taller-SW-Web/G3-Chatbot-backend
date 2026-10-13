"""SPEC-05 Req. 9 y ADR-0006: datos redactados antes de LLM/persistencia."""

import pytest

from src.domain.services.sensitive_data_filter import SensitiveDataFilter


@pytest.mark.parametrize("number", [
    "4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111",
    "4222222222222", "4000000000000000006", "0000000000000000",
])
def test_card_number_is_redacted_when_luhn_is_valid(number):
    result = SensitiveDataFilter().redactar(f"Mi tarjeta {number}, gracias")
    assert result.texto == "Mi tarjeta [tarjeta oculta], gracias"
    assert result.categorias == ("TARJETA",)
    assert "solo en la pantalla de pago" in result.avisos[0]
    assert number not in repr(result)


@pytest.mark.parametrize("label,number", [
    ("DNI", "12345678"), ("RUC:", "20123456789"), ("documento es", "12345678"),
    ("CE", "001234567"), ("carné de extranjería", "001234567"),
    ("pasaporte", "AB123456"), ("DNI número", "12345678"),
    ("documento de identidad es", "12345678"), ("DNI es el", "12345678"),
    ("DNI", "12.345.678"), ("RUC", "20-123-45678-9"), ("documento", "12 345 678"),
])
def test_labeled_identity_number_is_redacted(label, number):
    result = SensitiveDataFilter().redactar(f"Mi {label} {number} para pagar")
    assert number not in result.texto
    assert "[documento oculto]" in result.texto
    assert result.categorias == ("DOCUMENTO",)
    assert "documento se ingresa en la pantalla de pago" in result.avisos[0]


@pytest.mark.parametrize("texto", [
    "Pedido 12345678", "Celular 987654321", "Quiero 6 unidades",
    "carné de estudiante", "El documento está pendiente", "4111111111111112",
    "DNI 1234567", "RUC 12345678", "modelo ABC12345678",
    "DNI 12.345.6789",
    "Olvidé mi contraseña", "contraseña olvidada", "clave del producto", "recuperar password olvidada",
    "olvide mi clave ayer", "no recuerdo mi contrasena para entrar",
])
def test_non_sensitive_phrases_and_numbers_are_preserved(texto):
    result = SensitiveDataFilter().redactar(texto)
    assert result.texto == texto
    assert result.categorias == result.avisos == ()


def test_six_digit_otp_is_redacted_only_when_requested_by_conversation_state():
    filtro = SensitiveDataFilter()
    assert filtro.redactar("Código 123456").texto == "Código 123456"
    result = filtro.redactar("Código 123456 y pedido 12345678", otp_solicitado=True)
    assert result.texto == "Código [OTP oculto] y pedido 12345678"
    assert result.categorias == ("OTP",)


@pytest.mark.parametrize("texto", [
    "mi contraseña es Secreta123!", "password: ABC123", "clave = 'secreto con espacios'",
    "contraseña: ab.c!123",
])
def test_assigned_password_is_redacted(texto):
    result = SensitiveDataFilter().redactar(texto)
    assert "[contraseña oculta]" in result.texto
    assert result.categorias == ("CONTRASENA",)
    assert "Secreta123" not in repr(result)
    assert "ABC123" not in repr(result)
    assert "secreto con espacios" not in repr(result)
    assert "ab.c!123" not in repr(result)


def test_redaction_is_idempotent_and_does_not_retain_sensitive_values():
    filtro = SensitiveDataFilter()
    result = filtro.redactar(
        "DNI 12345678; tarjeta 4111 1111 1111 1111; password: test123; OTP 654321",
        otp_solicitado=True,
    )
    assert set(result.categorias) == {"DOCUMENTO", "TARJETA", "CONTRASENA", "OTP"}
    assert "12345678" not in repr(result) and "654321" not in repr(result)
    assert filtro.redactar(result.texto, otp_solicitado=True).texto == result.texto


@pytest.mark.parametrize("number", ["4111111111111111", "4111 1111 1111 1111", "4111.1111.1111.1111"])
def test_card_followed_by_quantity_redacts_only_the_card(number):
    result = SensitiveDataFilter().redactar(f"{number} 2 productos")
    assert result.texto == "[tarjeta oculta] 2 productos"


def test_two_cards_are_redacted_but_overlong_digit_sequences_are_not():
    filtro = SensitiveDataFilter()
    assert filtro.redactar("4111111111111111 378282246310005").texto == "[tarjeta oculta] [tarjeta oculta]"
    assert filtro.redactar("12345678901234567890").texto == "12345678901234567890"


def test_card_after_a_long_order_number_is_redacted_without_hiding_the_order():
    result = SensitiveDataFilter().redactar("Pedido 1234567890123 4111111111111111")
    assert result.texto == "Pedido 1234567890123 [tarjeta oculta]"


def test_identity_label_without_number_does_not_hide_a_different_document():
    result = SensitiveDataFilter().redactar("No tengo DNI pero mi RUC es 20123456789")
    assert result.texto == "No tengo DNI pero mi RUC es [documento oculto]"


@pytest.mark.parametrize("text,secret", [
    ("la contraseña que uso es abc123", "abc123"),
    ("mi contraseña de la cuenta es abc123", "abc123"),
    ("mi contraseña es Hola,Mundo123", "Hola,Mundo123"),
    ("password: Hola,Mundo123!", "Hola,Mundo123!"),
])
def test_password_assignment_redacts_the_complete_secret(text, secret):
    result = SensitiveDataFilter().redactar(text)
    assert secret not in result.texto
    assert secret.split(",")[-1].rstrip("!") not in result.texto
    assert result.texto.count("[contraseña oculta]") == 1
    assert result.categorias == ("CONTRASENA",)


@pytest.mark.parametrize("text", [
    "¿cómo cambio mi contraseña?",
    "la palabra clave es comodidad",
    "necesito un documento de compra del pedido 123456",
])
def test_password_questions_and_order_documents_are_not_redacted(text):
    result = SensitiveDataFilter().redactar(text)
    assert result.texto == text
    assert result.categorias == result.avisos == ()


def test_labeled_card_is_not_partially_redacted_as_an_identity_number():
    result = SensitiveDataFilter().redactar("documento 4111 1111 1111 1111")
    assert result.texto == "documento [tarjeta oculta]"
    assert result.categorias == ("TARJETA",)


def test_identity_number_is_redacted_before_sentence_punctuation():
    result = SensitiveDataFilter().redactar("mi DNI es 12345678.")
    assert result.texto == "mi DNI es [documento oculto]."


def test_redacting_a_document_twice_does_not_hide_a_later_order_reference():
    filtro = SensitiveDataFilter()
    first = filtro.redactar("mi DNI es 12345678 y mi pedido es PED12345")
    second = filtro.redactar(first.texto)
    assert second.texto == "mi DNI es [documento oculto] y mi pedido es PED12345"
    assert second.categorias == second.avisos == ()
