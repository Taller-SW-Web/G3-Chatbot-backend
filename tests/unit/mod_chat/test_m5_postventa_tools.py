"""M5 post-sale tools: lists, details, form preparation and confirmed registration."""

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from src.modules.mod_catalog.application.tools import (
    ClaimLookupArguments,
    ClaimPrepareArguments,
    EmptyArguments,
    ReturnLookupArguments,
    ReturnPrepareArguments,
)
from src.modules.mod_chat.application.tools.action_dispatcher import ActionDispatcher
from src.modules.mod_chat.application.tools.postventa_handlers import (
    build_postventa_confirmation_tools,
    build_postventa_handlers,
)
from src.modules.mod_postventa.adapters.fake_postventa import FakePostventaPort
from src.modules.mod_postventa.application.postventa_service import PostventaService
from src.shared.application.tools.tool_registry import ExecutionContext, ToolRegistry
from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort

CUSTOMER = uuid.uuid4()
OTHER = uuid.uuid4()
TOKEN = f"fake-access-{CUSTOMER}"
OTHER_TOKEN = f"fake-access-{OTHER}"
CONVERSATION = str(uuid.uuid4())
CONTEXT = ExecutionContext(conversacion_id=CONVERSATION, cliente_id=str(CUSTOMER), access_token=TOKEN)

CLAIM_TEXT = "La suela de la zapatilla derecha se despegó al primer uso."
CONSUMER = {
    "nombreCompleto": "Juan Pérez",
    "documento": "72458912",
    "email": "juan@example.com",
    "telefono": "+51999888777",
}


class FakeOrders:
    def __init__(self, orders: dict[str, dict[str, Any]]):
        self.orders = orders

    async def list_orders(self, customer_id, *, holder_token, estado=None, page=1, page_size=10):
        return {"items": list(self.orders.values()), "total": len(self.orders)}

    async def get_order_detail(self, customer_id, order_id, *, holder_token):
        row = self.orders.get(order_id)
        if row is None:
            raise ModuleRequestError("sales", 404, "RECURSO_NO_ENCONTRADO")
        return row


class FakeClaimRefs:
    def __init__(self):
        self.rows: dict[str, Any] = {}

    async def obtener_por_idempotency_key(self, key):
        return self.rows.get(key)

    async def guardar(self, row):
        self.rows[row.idempotency_key] = row


class FakeReturnRefs:
    def __init__(self):
        self.rows: dict[str, Any] = {}

    async def obtener_por_idempotency_key(self, key):
        return self.rows.get(key)

    async def guardar(self, row):
        self.rows[row.idempotency_key] = row


def _order(pedido_id: str = "PED-2026-00891", *, estado: str = "CREADO",
           history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "pedidoId": pedido_id,
        "estado": estado,
        "canal": "CHATBOT",
        "total": 129.9,
        "moneda": "PEN",
        "fechaCreacion": "2026-11-20T19:00:00Z",
        "envio": {"distrito": "Miraflores"},
        "items": [{"productoId": "PROD-1", "descripcion": "Zapatilla", "cantidad": 1}],
        "historial": history if history is not None else [
            {"estado": estado, "fecha": "2026-11-20T19:00:00Z", "completado": True},
        ],
    }


def _claim_payload(pedido_id: str = "PED-2026-00891", *, motivo: str = "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
                   forzar: bool = False) -> dict[str, Any]:
    return {
        "pedidoId": pedido_id,
        "tipo": "RECLAMO",
        "motivo": motivo,
        "descripcion": CLAIM_TEXT,
        "consumidor": dict(CONSUMER),
        "forzar": forzar,
    }


def _return_payload(pedido_id: str = "PED-2026-00891") -> dict[str, Any]:
    return {
        "pedidoId": pedido_id,
        "tipo": "CAMBIO",
        "motivo": "TALLA_INCORRECTA",
        "descripcion": "Me quedó chica.",
        "items": [{"productoId": "PROD-1", "cantidad": 1}],
        "evidencias": [],
    }


async def _sales_order(sales: FakeSalesPort, *, customer: uuid.UUID = CUSTOMER,
                       key: str = "k1") -> str:
    created = await sales.create_order(
        {"canal": "CHATBOT", "contacto": {"clienteId": str(customer)}, "items": [], "envio": {},
         "pago": {"total": 129.9}},
        idempotency_key=key,
    )
    return str(created["pedidoId"])


def _seed_claim(fake: FakePostventaPort, *, token: str = TOKEN, code: str = "REC-2026-0042",
                estado: str = "REGISTRADO", motivo: str = "PRODUCTO_DEFECTUOSO_O_INCORRECTO") -> dict[str, Any]:
    owner = token.removeprefix("fake-access-")
    row = {
        "reclamoId": code,
        "codigoSeguimiento": code,
        "pedidoId": "PED-2026-00891",
        "tipo": "RECLAMO",
        "canal": "CHATBOT",
        "motivo": motivo,
        "detalle": CLAIM_TEXT,
        "estado": estado,
        "plazoDiasHabiles": 15,
        "fechaLimiteSLA": "2026-12-16T00:00:00Z",
        "fechaRegistro": "2026-11-20T19:00:00Z",
        "respuestaVisibleCliente": None,
        "estadoSla": "EN_PLAZO",
        "diasHabilesRestantes": 15,
        "owner": owner,
    }
    fake.claims[code] = row
    return row


def _seed_return(fake: FakePostventaPort, *, token: str = TOKEN,
                 return_id: str = "DEV-2026-0042", estado: str = "EN_EVALUACION") -> dict[str, Any]:
    owner = token.removeprefix("fake-access-")
    row = {
        "devolucionId": return_id,
        "pedidoId": "PED-2026-00891",
        "tipo": "CAMBIO",
        "estado": estado,
        "motivo": "TALLA_INCORRECTA",
        "descripcion": "Me quedó chica.",
        "items": [{"productoId": "PROD-1", "cantidad": 1}],
        "evidencias": [],
        "fechaRegistro": "2026-11-20T19:00:00Z",
        "owner": owner,
        "resolucion": None,
        "historial": [],
        "recepciones": [],
    }
    fake.returns[return_id] = row
    return row


def _service(fake: FakePostventaPort, sales: FakeSalesPort | None = None) -> PostventaService:
    return PostventaService(
        postventa=fake,
        sales=sales or FakeSalesPort(),
        claim_refs=FakeClaimRefs(),
        return_refs=FakeReturnRefs(),
    )


def _confirm_registry(service: PostventaService) -> ToolRegistry:
    registry = ToolRegistry()
    for definition in build_postventa_confirmation_tools(service):
        registry.registrar(definition)
    return registry


@pytest.mark.asyncio
async def test_listar_reclamos_single_open_returns_estado_reclamo():
    fake = FakePostventaPort()
    _seed_claim(fake)
    handlers = build_postventa_handlers(_service(fake), FakeOrders({}))
    result = await handlers["listar_reclamos"](EmptyArguments(), CONTEXT)
    assert result.code == "OK"
    (block,) = result.data["bloques"]
    assert block["tipo"] == "ESTADO_RECLAMO"
    data = block["datos"]
    assert data["codigoSeguimiento"] == "REC-2026-0042"
    assert data["estadoEtiqueta"] == "Recibido"
    assert data["fechaLimiteSLA"] == "2026-12-16T00:00:00Z"


@pytest.mark.asyncio
async def test_listar_reclamos_several_returns_lista_reclamos():
    fake = FakePostventaPort()
    _seed_claim(fake)
    _seed_claim(fake, code="REC-2026-0043", motivo="ATENCION_INADEQUADA")
    handlers = build_postventa_handlers(_service(fake), FakeOrders({}))
    result = await handlers["listar_reclamos"](EmptyArguments(), CONTEXT)
    (block,) = result.data["bloques"]
    assert block["tipo"] == "LISTA_RECLAMOS"
    assert {row["codigoSeguimiento"] for row in block["datos"]["reclamos"]} == {
        "REC-2026-0042", "REC-2026-0043",
    }
    for row in block["datos"]["reclamos"]:
        assert {"codigoSeguimiento", "fecha", "pedidoId", "tipo", "motivo",
                "estado", "estadoEtiqueta"} <= set(row)


@pytest.mark.asyncio
async def test_consultar_reclamo_foreign_or_missing_is_not_found():
    fake = FakePostventaPort()
    _seed_claim(fake, token=OTHER_TOKEN)
    handlers = build_postventa_handlers(_service(fake), FakeOrders({}))
    result = await handlers["consultar_reclamo"](
        ClaimLookupArguments(codigoSeguimiento="REC-2026-0042"), CONTEXT)
    assert result.code == "OK"
    assert "No encontré ese reclamo" in result.data["texto"]
    assert result.data["bloques"][0]["datos"]["acciones"][0]["id"] == "MIS_RECLAMOS"


@pytest.mark.asyncio
async def test_consultar_reclamo_shows_the_verbatim_answer():
    fake = FakePostventaPort()
    row = _seed_claim(fake, estado="ATENDIDO")
    row["respuestaVisibleCliente"] = "Aprobamos la reposición del producto."
    handlers = build_postventa_handlers(_service(fake), FakeOrders({}))
    result = await handlers["consultar_reclamo"](
        ClaimLookupArguments(codigoSeguimiento="REC-2026-0042"), CONTEXT)
    (block,) = result.data["bloques"]
    assert block["tipo"] == "ESTADO_RECLAMO"
    assert block["datos"]["respuestaVisibleCliente"] == "Aprobamos la reposición del producto."
    assert block["datos"]["estadoEtiqueta"] == "Respondido"


@pytest.mark.asyncio
async def test_listar_devoluciones_maps_estado_reembolso():
    fake = FakePostventaPort()
    row = _seed_return(fake)
    row["estadoReembolso"] = "EN_PROCESO"
    handlers = build_postventa_handlers(_service(fake), FakeOrders({}))
    result = await handlers["listar_devoluciones"](EmptyArguments(), CONTEXT)
    (block,) = result.data["bloques"]
    assert block["tipo"] == "LISTA_DEVOLUCIONES"
    [item] = block["datos"]["devoluciones"]
    assert item["devolucionId"] == "DEV-2026-0042"
    assert item["estadoEtiqueta"] == "En revisión"
    assert item["estadoReembolso"] == "EN_PROCESO"


@pytest.mark.asyncio
async def test_consultar_devolucion_whitelists_resolucion_and_refund():
    fake = FakePostventaPort()
    row = _seed_return(fake, estado="APROBADA")
    row["resolucion"] = {
        "fundamento": "Producto defectuoso acreditado.",
        "reembolso": {
            "reembolsoId": "REB-1", "estado": "EXITOSO", "monto": 129.9,
            "moneda": "PEN", "fechaEjecucion": "2026-11-22T16:01:10Z",
            "transaccionPasarelaId": "interno",
        },
    }
    handlers = build_postventa_handlers(_service(fake), FakeOrders({}))
    result = await handlers["consultar_devolucion"](
        ReturnLookupArguments(devolucionId="DEV-2026-0042"), CONTEXT)
    (block,) = result.data["bloques"]
    assert block["tipo"] == "ESTADO_DEVOLUCION"
    resolucion = block["datos"]["resolucion"]
    assert resolucion["fundamento"] == "Producto defectuoso acreditado."
    assert resolucion["reembolso"] == {
        "reembolsoId": "REB-1", "estado": "EXITOSO", "monto": 129.9,
        "moneda": "PEN", "fechaEjecucion": "2026-11-22T16:01:10Z",
    }
    assert "transaccionPasarelaId" not in str(block)


@pytest.mark.asyncio
async def test_consultar_devolucion_foreign_or_missing_is_not_found():
    fake = FakePostventaPort()
    _seed_return(fake, token=OTHER_TOKEN)
    handlers = build_postventa_handlers(_service(fake), FakeOrders({}))
    result = await handlers["consultar_devolucion"](
        ReturnLookupArguments(devolucionId="DEV-2026-0042"), CONTEXT)
    assert result.code == "OK"
    assert "No encontré esa solicitud" in result.data["texto"]
    assert result.data["bloques"][0]["datos"]["acciones"][0]["id"] == "MIS_DEVOLUCIONES"


@pytest.mark.asyncio
async def test_preparar_reclamo_returns_a_form_without_registering():
    fake = FakePostventaPort()
    orders = FakeOrders({"PED-2026-00891": _order()})
    handlers = build_postventa_handlers(_service(fake), orders)
    result = await handlers["preparar_reclamo"](
        ClaimPrepareArguments(
            pedidoRef="PED-2026-00891", tipo="RECLAMO",
            motivo="PRODUCTO_DEFECTUOSO_O_INCORRECTO", descripcion=CLAIM_TEXT,
        ), CONTEXT)
    (block,) = result.data["bloques"]
    assert block["tipo"] == "FORMULARIO" and block["formulario"] == "RECLAMO"
    assert block["datos"]["pedidoId"] == "PED-2026-00891"
    assert block["datos"]["motivo"] == "PRODUCTO_DEFECTUOSO_O_INCORRECTO"
    assert fake.claims == {}


@pytest.mark.asyncio
async def test_preparar_reclamo_without_order_lists_orders_to_choose():
    fake = FakePostventaPort()
    orders = FakeOrders({"PED-2026-00891": _order(), "PED-2026-00892": _order("PED-2026-00892")})
    handlers = build_postventa_handlers(_service(fake), orders)
    result = await handlers["preparar_reclamo"](ClaimPrepareArguments(), CONTEXT)
    (block,) = result.data["bloques"]
    assert block["tipo"] == "LISTA_PEDIDOS"
    assert len(block["datos"]["pedidos"]) == 2


@pytest.mark.asyncio
async def test_preparar_reclamo_foreign_order_is_not_found():
    fake = FakePostventaPort()
    handlers = build_postventa_handlers(_service(fake), FakeOrders({}))
    result = await handlers["preparar_reclamo"](
        ClaimPrepareArguments(pedidoRef="PED-2026-99999"), CONTEXT)
    assert result.code == "OK"
    assert "No encontré ese pedido" in result.data["texto"]
    assert fake.claims == {}


@pytest.mark.asyncio
async def test_preparar_devolucion_requires_a_delivered_order():
    fake = FakePostventaPort()
    orders = FakeOrders({"PED-2026-00891": _order(estado="EN_PREPARACION")})
    handlers = build_postventa_handlers(_service(fake), orders)
    result = await handlers["preparar_devolucion"](
        ReturnPrepareArguments(pedidoRef="PED-2026-00891", tipo="CAMBIO"), CONTEXT)
    assert "después de la entrega" in result.data["texto"]
    assert result.data["bloques"][0]["datos"]["acciones"][0]["id"] == "CONSULTAR_SEGUIMIENTO"
    assert fake.returns == {}


@pytest.mark.asyncio
async def test_preparar_devolucion_prefills_the_form_for_a_delivered_order():
    fake = FakePostventaPort()
    today = datetime.now(timezone.utc).isoformat()
    orders = FakeOrders({"PED-2026-00891": _order(
        estado="ENTREGADO",
        history=[{"estado": "ENTREGADO", "fecha": today, "completado": True}],
    )})
    handlers = build_postventa_handlers(_service(fake), orders)
    result = await handlers["preparar_devolucion"](
        ReturnPrepareArguments(
            pedidoRef="PED-2026-00891", lineaRef="zapatillas", tipo="CAMBIO",
            motivo="TALLA_INCORRECTA", varianteDeseada="42", descripcion="Me quedó chica.",
        ), CONTEXT)
    (block,) = result.data["bloques"]
    assert block["tipo"] == "FORMULARIO" and block["formulario"] == "DEVOLUCION"
    assert block["datos"]["lineaRef"] == "zapatillas"
    assert block["datos"]["varianteDeseada"] == "42"
    assert block["datos"]["items"][0]["productoId"] == "PROD-1"
    assert fake.returns == {}


@pytest.mark.asyncio
async def test_preparar_devolucion_rejects_the_expired_window():
    fake = FakePostventaPort()
    orders = FakeOrders({"PED-2026-00891": _order(
        estado="ENTREGADO",
        history=[{"estado": "ENTREGADO", "fecha": "2026-01-01T00:00:00Z", "completado": True}],
    )})
    handlers = build_postventa_handlers(_service(fake), orders)
    result = await handlers["preparar_devolucion"](
        ReturnPrepareArguments(pedidoRef="PED-2026-00891", tipo="CAMBIO"), CONTEXT)
    assert "plazo de 7 días" in result.data["texto"]
    assert fake.returns == {}


@pytest.mark.asyncio
async def test_confirmar_reclamo_registers_once_and_returns_the_constancia():
    sales = FakeSalesPort()
    order_id = await _sales_order(sales)
    fake = FakePostventaPort()
    service = _service(fake, sales)
    registry = _confirm_registry(service)
    dispatcher = ActionDispatcher(registry)
    action = {"tipo": "CONFIRMAR_RECLAMO", "payload": _claim_payload(order_id)}
    pending = await dispatcher.despachar(action, CONTEXT)
    assert pending.code == "REQUIERE_CONFIRMACION"
    assert fake.claims == {}
    first = await dispatcher.despachar(action, CONTEXT, confirmacion_ui=True)
    assert first.code == "OK"
    (block,) = first.data["bloques"]
    assert block["tipo"] == "CONSTANCIA_RECLAMO"
    data = block["datos"]
    assert data["pedidoId"] == order_id
    assert data["motivo"] == "PRODUCTO_DEFECTUOSO_O_INCORRECTO"
    assert data["mensaje"] == "Guarda este código para consultar tu reclamo."
    assert data["fechaLimiteSLA"]
    second = await dispatcher.despachar(action, CONTEXT, confirmacion_ui=True)
    assert second.code == "OK"
    assert second.data["bloques"][0]["datos"]["codigoSeguimiento"] == data["codigoSeguimiento"]
    assert len(fake.claims) == 1


@pytest.mark.asyncio
async def test_confirmar_reclamo_foreign_order_does_not_register():
    sales = FakeSalesPort()
    foreign = await _sales_order(sales, customer=OTHER, key="other")
    fake = FakePostventaPort()
    service = _service(fake, sales)
    dispatcher = ActionDispatcher(_confirm_registry(service))
    result = await dispatcher.despachar(
        {"tipo": "CONFIRMAR_RECLAMO", "payload": _claim_payload(foreign)},
        CONTEXT, confirmacion_ui=True,
    )
    assert result.code == "RECURSO_NO_ENCONTRADO"
    assert fake.claims == {}


@pytest.mark.asyncio
async def test_confirmar_devolucion_registers_and_returns_the_constancia():
    sales = FakeSalesPort()
    order_id = await _sales_order(sales)
    fake = FakePostventaPort()
    service = _service(fake, sales)
    dispatcher = ActionDispatcher(_confirm_registry(service))
    action = {"tipo": "CONFIRMAR_DEVOLUCION", "payload": _return_payload(order_id)}
    result = await dispatcher.despachar(action, CONTEXT, confirmacion_ui=True)
    assert result.code == "OK"
    (block,) = result.data["bloques"]
    assert block["tipo"] == "CONSTANCIA_DEVOLUCION"
    assert block["datos"]["devolucionId"].startswith("DEV-2026-")
    assert block["datos"]["estadoEtiqueta"] == "Recibida"
    assert len(fake.returns) == 1


@pytest.mark.asyncio
async def test_confirm_actions_pass_the_conversation_id_to_postventa():
    sales = FakeSalesPort()
    order_id = await _sales_order(sales)
    fake = FakePostventaPort()
    service = _service(fake, sales)
    calls: list[tuple[str, uuid.UUID | None]] = []
    original_claim = service.create_claim
    original_return = service.create_return

    async def record_claim(**kwargs):
        calls.append(("claim", kwargs.get("conversation_id")))
        return await original_claim(**kwargs)

    async def record_return(**kwargs):
        calls.append(("return", kwargs.get("conversation_id")))
        return await original_return(**kwargs)

    service.create_claim = record_claim  # type: ignore[method-assign]
    service.create_return = record_return  # type: ignore[method-assign]
    dispatcher = ActionDispatcher(_confirm_registry(service))

    await dispatcher.despachar(
        {"tipo": "CONFIRMAR_RECLAMO", "payload": _claim_payload(order_id)},
        CONTEXT, confirmacion_ui=True,
    )
    await dispatcher.despachar(
        {"tipo": "CONFIRMAR_DEVOLUCION", "payload": _return_payload(order_id)},
        CONTEXT, confirmacion_ui=True,
    )

    expected = uuid.UUID(CONVERSATION)
    assert calls == [("claim", expected), ("return", expected)]


@pytest.mark.asyncio
async def test_confirm_actions_without_conversation_do_not_register():
    no_conversation = ExecutionContext(
        conversacion_id=None, cliente_id=str(CUSTOMER), access_token=TOKEN
    )
    sales = FakeSalesPort()
    order_id = await _sales_order(sales)
    fake = FakePostventaPort()
    dispatcher = ActionDispatcher(_confirm_registry(_service(fake, sales)))

    claim = await dispatcher.despachar(
        {"tipo": "CONFIRMAR_RECLAMO", "payload": _claim_payload(order_id)},
        no_conversation, confirmacion_ui=True,
    )
    assert claim.code == "REQUIERE_SESION"
    assert fake.claims == {}

    returned = await dispatcher.despachar(
        {"tipo": "CONFIRMAR_DEVOLUCION", "payload": _return_payload(order_id)},
        no_conversation, confirmacion_ui=True,
    )
    assert returned.code == "REQUIERE_SESION"
    assert fake.returns == {}


@pytest.mark.asyncio
async def test_confirm_actions_are_not_in_the_llm_catalog():
    sales = FakeSalesPort()
    fake = FakePostventaPort()
    registry = _confirm_registry(_service(fake, sales))
    assert registry.catalogo() == []
    order_id = await _sales_order(sales)
    llm = await registry.ejecutar("confirmar_reclamo", _claim_payload(order_id), CONTEXT)
    assert llm.code == "HERRAMIENTA_DESCONOCIDA"
    assert fake.claims == {}
