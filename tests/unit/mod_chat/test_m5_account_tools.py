"""M5 account, checkout and navigation tools return UI surfaces, never side effects."""

import uuid

import pytest

from src.modules.mod_cart.public import CartError
from src.modules.mod_catalog.application.tools import ConversationSearchArguments, EmptyArguments
from src.modules.mod_chat.application.tools.account_handlers import build_account_handlers
from src.modules.mod_chat.application.tools.cart_handlers import build_cart_handlers
from src.shared.application.tools.tool_registry import ExecutionContext

CUSTOMER = str(uuid.uuid4())
CONVERSATION = str(uuid.uuid4())
AUTHENTICATED = ExecutionContext(conversacion_id=CONVERSATION, cliente_id=CUSTOMER)
ANONYMOUS = ExecutionContext(conversacion_id=CONVERSATION)


@pytest.mark.asyncio
async def test_solicitar_login_opens_the_form_only_for_anonymous_customers():
    handlers = build_account_handlers()
    anonymous = await handlers["solicitar_login"](EmptyArguments(), ANONYMOUS)
    assert anonymous.code == "OK"
    (block,) = anonymous.data["bloques"]
    assert block["tipo"] == "FORMULARIO" and block["formulario"] == "LOGIN"
    logged = await handlers["solicitar_login"](EmptyArguments(), AUTHENTICATED)
    assert logged.code == "OK"
    assert "sesión iniciada" in logged.data["texto"]
    assert "bloques" not in logged.data


@pytest.mark.asyncio
async def test_reenviar_verificacion_opens_the_resend_form():
    result = await build_account_handlers()["reenviar_verificacion"](EmptyArguments(), ANONYMOUS)
    assert result.code == "OK"
    (block,) = result.data["bloques"]
    assert block["tipo"] == "FORMULARIO" and block["formulario"] == "REENVIO_VERIFICACION"


@pytest.mark.asyncio
async def test_iniciar_checkout_returns_the_client_action_and_no_checkout():
    result = await build_account_handlers()["iniciar_checkout"](EmptyArguments(), AUTHENTICATED)
    assert result.code == "OK"
    (block,) = result.data["bloques"]
    assert block["tipo"] == "ACCIONES_RAPIDAS"
    assert block["datos"]["acciones"] == [
        {"id": "IR_A_CHECKOUT", "etiqueta": "Ir al checkout", "payload": {}}
    ]


@pytest.mark.asyncio
async def test_iniciar_checkout_without_session_keeps_the_pending_action():
    result = await build_account_handlers()["iniciar_checkout"](EmptyArguments(), ANONYMOUS)
    assert result.code == "REQUIERE_SESION"
    assert result.data["accionPendiente"] == "INICIAR_CHECKOUT"
    assert result.data["bloques"][0]["formulario"] == "LOGIN"


@pytest.mark.asyncio
async def test_cerrar_sesion_returns_the_client_action_without_touching_cookies():
    result = await build_account_handlers()["cerrar_sesion"](EmptyArguments(), AUTHENTICATED)
    assert result.code == "OK"
    (block,) = result.data["bloques"]
    assert block["datos"]["acciones"][0]["id"] == "CERRAR_SESION"
    assert "cookie" not in repr(result).lower()


@pytest.mark.asyncio
async def test_nueva_conversacion_only_returns_the_client_action():
    result = await build_account_handlers()["nueva_conversacion"](EmptyArguments(), AUTHENTICATED)
    assert result.code == "OK"
    assert result.data["bloques"][0]["datos"]["acciones"][0]["id"] == "NUEVO_CHAT"


@pytest.mark.asyncio
async def test_buscar_conversaciones_returns_the_query_in_the_action_payload():
    result = await build_account_handlers()["buscar_conversaciones"](
        ConversationSearchArguments(q="  zapatillas  "), ANONYMOUS)
    assert result.code == "OK"
    action = result.data["bloques"][0]["datos"]["acciones"][0]
    assert action["id"] == "BUSCAR_CHATS"
    assert action["payload"] == {"q": "zapatillas"}


class FakeCartUseCase:
    async def apply_coupon(self, caller, conversation_id, *, codigo, confirmar_reemplazo=False):
        if codigo == "OTRO" and not confirmar_reemplazo:
            raise CartError("REQUIERE_CONFIRMACION_REEMPLAZO", "Tu carrito ya tiene un cupón aplicado.",
                            {"cuponActual": "VIEJO", "cuponNuevo": codigo})
        return {
            "code": "OK",
            "texto": f"Cupón {codigo} aplicado.",
            "cupon": {"codigo": codigo, "descuento": 10.0, "moneda": "PEN"},
            "bloques": [{"id": "b1", "tipo": "CARRITO", "datos": {"lineas": []}}],
        }

    async def remove_coupon(self, caller, conversation_id):
        return {
            "code": "OK",
            "texto": "Quitamos el cupón de tu carrito.",
            "cupon": None,
            "bloques": [{"id": "b2", "tipo": "CARRITO", "datos": {"lineas": []}}],
        }


@pytest.mark.asyncio
async def test_coupon_tools_reuse_the_cart_use_case():
    handlers = build_cart_handlers(FakeCartUseCase())
    applied = await handlers["aplicar_cupon"](
        type("Args", (), {"codigo": "RUN10", "confirmarReemplazo": False})(), AUTHENTICATED)
    assert applied.code == "OK"
    assert applied.data["cupon"]["codigo"] == "RUN10"
    assert applied.data["bloques"][0]["tipo"] == "CARRITO"
    replacement = await handlers["aplicar_cupon"](
        type("Args", (), {"codigo": "OTRO", "confirmarReemplazo": False})(), AUTHENTICATED)
    assert replacement.code == "REQUIERE_CONFIRMACION_REEMPLAZO"
    assert replacement.data["cuponActual"] == "VIEJO"
    removed = await handlers["quitar_cupon"](
        type("Args", (), {})(), AUTHENTICATED)
    assert removed.code == "OK"
    assert removed.data["cupon"] is None

