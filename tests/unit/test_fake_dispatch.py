"""Despacho fake (`DISPATCH_MODE=fake`): cotización y seguimiento sin G2.

Es el modo que usa la demo desplegada: cubre los distritos del checkout con
S/ 12.50 y un día, responde sin cobertura para el resto y no toca la red.
"""
import pytest

from src.adapters.outbound.fakes.fake_dispatch import (
    COSTO_ENVIO,
    PLAZO_DIAS,
    FakeDispatchPort,
)
from src.infrastructure.config.container import provide_despacho_client
from src.infrastructure.config.settings import Settings
from src.ports.outbound.module_errors import ModuleRequestError

DESTINO = {
    "destinatario": "Ana Díaz",
    "direccion": "Av. Larco 1234, dpto. 502",
    "distrito": "Miraflores",
    "departamento": "Lima",
    "provincia": "Lima",
    "referencia": "Frente al parque",
}
LINEAS = [{"sku": "NIK-REV7-42-NEG", "cantidad": 1}]


@pytest.fixture
def fake() -> FakeDispatchPort:
    return FakeDispatchPort()


@pytest.mark.asyncio
async def test_cotiza_con_cobertura_y_los_valores_de_la_demo(fake):
    cotizacion = await fake.quote_shipping(destino=DESTINO, lineas=LINEAS)

    assert cotizacion["coberturaDisponible"] is True
    assert cotizacion["costoEnvio"] == COSTO_ENVIO
    assert cotizacion["plazoEstimadoDiasHabiles"] == PLAZO_DIAS
    assert cotizacion["moneda"] == "PEN"
    assert cotizacion["distrito"] == "Miraflores"
    assert cotizacion["nombreZona"] == "Lima Metropolitana"
    assert cotizacion["fechaEstimadaEntrega"]


@pytest.mark.asyncio
async def test_responde_sin_cobertura_para_un_distrito_desconocido(fake):
    cotizacion = await fake.quote_shipping(
        destino={**DESTINO, "distrito": "Trujillo"}, lineas=LINEAS
    )

    assert cotizacion["coberturaDisponible"] is False
    assert "costoEnvio" not in cotizacion


@pytest.mark.asyncio
async def test_exige_destinatario_y_direccion(fake):
    with pytest.raises(ModuleRequestError) as exc:
        await fake.quote_shipping(destino={"distrito": "Miraflores"}, lineas=LINEAS)

    assert exc.value.code == "DESP_ERROR_DESTINO_REQUERIDO"


@pytest.mark.asyncio
async def test_exige_lineas(fake):
    with pytest.raises(ModuleRequestError) as exc:
        await fake.quote_shipping(destino=DESTINO, lineas=[])

    assert exc.value.code == "DESP_ERROR_LINEAS_VACIAS"


@pytest.mark.asyncio
async def test_seguimiento_de_un_pedido_conocido(fake):
    seguimiento = await fake.get_order_tracking("PED-2026-00891")

    assert seguimiento is not None
    assert seguimiento["estado"] == "EN_CAMINO"
    assert len(seguimiento["hitos"]) == 2
    assert all(hito["completado"] for hito in seguimiento["hitos"])


@pytest.mark.asyncio
async def test_seguimiento_desconocido_es_none(fake):
    assert await fake.get_order_tracking("OTRO-123") is None


def test_el_contenedor_usa_el_fake_cuando_el_modo_lo_pide():
    settings = Settings(modules_mode="fake")

    port = provide_despacho_client(settings)

    assert isinstance(port, FakeDispatchPort)
    assert "dispatch" in settings.fake_modules()


def test_el_override_por_modulo_gana_al_modo_global():
    settings = Settings(modules_mode="http", dispatch_mode="fake")

    assert isinstance(provide_despacho_client(settings), FakeDispatchPort)
