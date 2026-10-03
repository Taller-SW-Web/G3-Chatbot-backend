"""SPEC-05 Req. 7: precios respaldados y corrección en el texto para fin."""

from decimal import Decimal

import pytest

from src.domain.services.output_validator import OutputValidator


@pytest.mark.parametrize("texto,monto", [
    ("Precio S/ 289.90", "289.90"), ("Total PEN 99,90", "99.90"),
    ("Cuesta 299 soles", "299"), ("S/ 1,299.90", "1299.90"),
    ("S/ 1.299,90", "1299.90"), ("Total S/ 1,299", "1299"),
    ("S/. 0.10", "0.10"),
    ("S/ 1 299.90", "1299.90"), ("Descuento S/ -10.00", "-10.00"),
])
def test_precio_coincide_con_herramienta(texto, monto):
    result = OutputValidator().validar(texto, montos=[Decimal(monto)])
    assert result.texto == texto and result.corregido is False


def test_precio_inventado_se_corrige_sin_inventar_reemplazo():
    result = OutputValidator().validar("Cuesta S/ 199.90 y el total es S/ 299.90", montos=[Decimal("299.90")])
    assert result.texto == "Cuesta [importe no verificado] y el total es S/ 299.90"
    assert result.corregido


def test_sin_resultados_no_acepta_precio_ni_descuento():
    result = OutputValidator().validar("Cuesta 10 soles con 100% de descuento", montos=[])
    assert result.texto == "Cuesta [importe no verificado] con [descuento no verificado] de descuento"
    assert result.corregido


def test_porcentaje_respaldado_y_numero_de_talla_sin_moneda():
    text = "Talla 42 con 10% de descuento"
    result = OutputValidator().validar(text, montos=[], porcentajes=[Decimal("10")])
    assert result.texto == text and not result.corregido


def test_precio_negativo_y_miles_con_espacio_no_evaden_validacion():
    result = OutputValidator().validar("S/ -100.00 y S/ 100 000.00", montos=[Decimal("100")])
    assert result.texto == "[importe no verificado] y [importe no verificado]"
    assert result.corregido


def test_importes_malformados_no_se_aceptan_por_coincidencia_parcial():
    result = OutputValidator().validar(
        "S/ 1234.567; S/ 1.234,567.", montos=[Decimal("1234")],
    )
    assert result.texto == "[importe no verificado]; [importe no verificado]."
    assert result.corregido


def test_monto_valido_se_valida_aunque_cierre_la_oracion():
    text = "El precio es S/ 1.234,90."
    result = OutputValidator().validar(text, montos=[Decimal("1234.90")])
    assert result.texto == text and not result.corregido


@pytest.mark.parametrize("texto", [
    "Polo 100% algodón", "Es 100% original", "100% algodón con 10% de descuento",
])
def test_porcentaje_textil_no_se_confunde_con_descuento(texto):
    result = OutputValidator().validar(texto, montos=[], porcentajes=[Decimal("10")])
    assert result.texto == texto and not result.corregido


@pytest.mark.parametrize("texto", ["Descuento de 100%", "Ofertas: 100%", "100% de descuento", "20% off"])
def test_descuentos_sin_respaldo_se_corrigen(texto):
    result = OutputValidator().validar(texto, montos=[])
    assert result.corregido and "%" not in result.texto
