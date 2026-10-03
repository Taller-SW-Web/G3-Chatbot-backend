"""SPEC-05 Req. 9 y ADR-0006: datos redactados antes de LLM/persistencia."""

import pytest

from src.domain.services.sensitive_data_filter import SensitiveDataFilter


@pytest.mark.parametrize("numero", [
    "4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111",
    "4222222222222", "4000000000000000006", "0000000000000000",
])
def test_cliente_escribe_tarjeta_luhn(numero):
    result = SensitiveDataFilter().redactar(f"Mi tarjeta {numero}, gracias")
    assert result.texto == "Mi tarjeta [tarjeta oculta], gracias"
    assert result.categorias == ("TARJETA",)
    assert "solo en la pantalla de pago" in result.avisos[0]
    assert numero not in repr(result)


@pytest.mark.parametrize("label,numero", [
    ("DNI", "12345678"), ("RUC:", "20123456789"), ("documento es", "12345678"),
    ("CE", "001234567"), ("carné de extranjería", "001234567"),
    ("pasaporte", "AB123456"), ("DNI número", "12345678"),
    ("documento de identidad es", "12345678"), ("DNI es el", "12345678"),
    ("DNI", "12.345.678"), ("RUC", "20-123-45678-9"), ("documento", "12 345 678"),
])
def test_documento_precedido_por_palabra_identificadora(label, numero):
    result = SensitiveDataFilter().redactar(f"Mi {label} {numero} para pagar")
    assert numero not in result.texto
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
def test_secuencias_y_palabras_no_sensibles_se_conservan(texto):
    result = SensitiveDataFilter().redactar(texto)
    assert result.texto == texto
    assert result.categorias == result.avisos == ()


def test_otp_de_seis_digitos_solo_tras_pedir_codigo():
    filtro = SensitiveDataFilter()
    assert filtro.redactar("Código 123456").texto == "Código 123456"
    result = filtro.redactar("Código 123456 y pedido 12345678", otp_solicitado=True)
    assert result.texto == "Código [OTP oculto] y pedido 12345678"
    assert result.categorias == ("OTP",)


@pytest.mark.parametrize("texto", [
    "mi contraseña es Secreta123!", "password: ABC123", "clave = 'secreto con espacios'",
    "contraseña: ab.c!123",
])
def test_patrones_de_contrasena(texto):
    result = SensitiveDataFilter().redactar(texto)
    assert "[contraseña oculta]" in result.texto
    assert result.categorias == ("CONTRASENA",)
    assert "Secreta123" not in repr(result)
    assert "ABC123" not in repr(result)
    assert "secreto con espacios" not in repr(result)
    assert "ab.c!123" not in repr(result)


def test_mezcla_e_idempotencia_sin_guardar_original():
    filtro = SensitiveDataFilter()
    result = filtro.redactar(
        "DNI 12345678; tarjeta 4111 1111 1111 1111; password: test123; OTP 654321",
        otp_solicitado=True,
    )
    assert set(result.categorias) == {"DOCUMENTO", "TARJETA", "CONTRASENA", "OTP"}
    assert "12345678" not in repr(result) and "654321" not in repr(result)
    assert filtro.redactar(result.texto, otp_solicitado=True).texto == result.texto


@pytest.mark.parametrize("numero", ["4111111111111111", "4111 1111 1111 1111"])
def test_tarjeta_seguida_por_cantidad_no_se_filtra(numero):
    result = SensitiveDataFilter().redactar(f"{numero} 2 productos")
    assert result.texto == "[tarjeta oculta] 2 productos"


def test_dos_tarjetas_seguidas_y_secuencia_demasiado_larga():
    filtro = SensitiveDataFilter()
    assert filtro.redactar("4111111111111111 378282246310005").texto == "[tarjeta oculta] [tarjeta oculta]"
    assert filtro.redactar("12345678901234567890").texto == "12345678901234567890"


def test_etiqueta_sin_numero_no_oculta_otro_tipo_de_documento():
    result = SensitiveDataFilter().redactar("No tengo DNI pero mi RUC es 20123456789")
    assert result.texto == "No tengo DNI pero mi RUC es [documento oculto]"
