"""H3-05 contracts: settings per environment, base HTTP client, tool catalog, blocks/SSE DTOs."""
import httpx
import pytest
import respx

from src.adapters.outbound.http_clients.base_client import BaseModuleClient, ModuleUnavailableError
from src.application.dto.chatbot_response_dto import AssistantTurnResult, ChatBlock
from src.application.dto.incoming_message_dto import SendMessageRequest
from src.application.tools.tool_contracts import HITO3_TOOL_CONTRACTS, get_tool_contract
from src.domain.value_objects.block import BlockType, FormKind, QuickActionId
from src.infrastructure.config.container import provide_module_client
from src.infrastructure.config.settings import get_settings


def test_settings_module_urls_from_env(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("PRODUCTS_BASE_URL", "http://mock:4011/")
    settings = get_settings()
    assert settings.products_base_url == "http://mock:4011"
    assert settings.products_timeout_seconds == 4.0
    assert settings.security_timeout_seconds == 5.0
    get_settings.cache_clear()


def test_normalize_codigo_to_code():
    assert BaseModuleClient.normalize_code({"codigo": "X", "a": 1})["code"] == "X"


def test_module_unavailable_on_timeout():
    client = BaseModuleClient(base_url="http://x", timeout_seconds=0.01)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))))
    with pytest.raises(ModuleUnavailableError):
        import anyio
        anyio.run(client._request, "GET", "/ping")


@respx.mock
def test_request_carries_correlation_id():
    import anyio

    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    respx.get("http://mock:4011/ping").mock(side_effect=handler)
    client = BaseModuleClient(base_url="http://mock:4011", timeout_seconds=5.0)
    anyio.run(client._request, "GET", "/ping", "abc123")
    assert seen["x-correlation-id"] == "abc123"


def test_tool_catalog_requires_session_and_confirmation():
    assert get_tool_contract("vaciar_carrito").requires_confirmation is True
    assert get_tool_contract("iniciar_checkout").requires_session is True
    assert get_tool_contract("buscar_productos").requires_session is False
    assert len(HITO3_TOOL_CONTRACTS) == 16
    with pytest.raises(KeyError):
        get_tool_contract("no_existe")


def test_block_types_cover_contract():
    assert BlockType.CARRUSEL_PRODUCTOS.value == "CARRUSEL_PRODUCTOS"
    assert FormKind.LOGIN.value == "LOGIN"
    assert QuickActionId.VER_OFERTAS.value == "VER_OFERTAS"


def test_send_message_shapes():
    assert SendMessageRequest(texto="hola").texto == "hola"
    assert SendMessageRequest(accion={"tipo": "VER_OFERTAS", "payload": {}}).accion.tipo == "VER_OFERTAS"
    with pytest.raises(Exception):
        SendMessageRequest(texto="a", accion={"tipo": "X", "payload": {}})
    with pytest.raises(Exception):
        SendMessageRequest(texto="x" * 1001)


def test_assistant_turn_result_defaults():
    turn = AssistantTurnResult(mensajeId="m1")
    assert turn.bloques == [] and turn.degradado is None
    turn2 = AssistantTurnResult(
        mensajeId="m2", bloques=[ChatBlock(id="b1", tipo=BlockType.ACCIONES_RAPIDAS)]
    )
    assert turn2.bloques[0].tipo is BlockType.ACCIONES_RAPIDAS


def test_provide_module_client_wiring():
    client = provide_module_client("security")
    assert client.base_url.startswith("http")
    assert client.timeout_seconds == 5.0
    with pytest.raises(ValueError):
        provide_module_client("unknown")
