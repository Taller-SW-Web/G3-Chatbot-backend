"""H3-10: HTTP edge of SPEC-05 Req. 1 with dependency overrides (no database)."""
import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.inbound.http.chatbot_router import create_chatbot_router
from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.adapters.inbound.http.health_router import router as health_router
from src.adapters.inbound.http.problem import register_problem_handlers
from src.domain.entities.conversation import Conversation, ConversationSummary
from src.domain.entities.message import Message, MessageRole
from src.domain.value_objects.caller_identity import CallerIdentity
from src.infrastructure.main import create_app
from src.ports.inbound.chatbot_service_port import ConversationNotFoundError

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
CID = uuid.uuid4()


class FakeService:
    def __init__(self):
        self.created_for = None
        self.list_call = None
        self.search_call = None

    async def create_conversation(self, caller):
        self.created_for = caller
        return Conversation(id=CID, title=None, last_message_at=NOW)

    async def list_conversations(self, caller, page):
        self.list_call = (caller, page)
        if caller is None:
            return []
        return [ConversationSummary(id=CID, title="Zapatillas", preview="Talla 42", last_message_at=NOW)]

    async def search_conversations(self, caller, query, page):
        self.search_call = (caller, query, page)
        if caller is None:
            return []
        return [ConversationSummary(id=CID, title="Zapatillas", preview="Talla 42", last_message_at=NOW)]

    async def get_history(self, caller, conversation_id):
        if caller is None or conversation_id != CID:
            raise ConversationNotFoundError()
        return [
            Message(id=uuid.uuid4(), conversation_id=CID, role=MessageRole.CUSTOMER, text="hola",
                    blocks=None, created_at=NOW),
            Message(id=uuid.uuid4(), conversation_id=CID, role=MessageRole.ASSISTANT, text="buenas",
                    blocks={"x": 1}, created_at=NOW),
        ]


@pytest.fixture
def service():
    return FakeService()


def make_client(service, *, cookie_secure=True, identity=None):
    """`identity=None` uses the real cookie-based dependency."""
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(
        create_chatbot_router(lambda: service, cookie_secure=cookie_secure), prefix="/api/v1"
    )
    if identity is not None:
        app.dependency_overrides[get_caller_identity] = lambda: identity
    return TestClient(app)


BASE = "/api/v1/chat/conversaciones"


def test_nueva_conversacion_sin_cookie_genera_chat_sid_http_only(service):
    """SPEC-05 Req. 1 · Scenario: Nueva conversación desde la barra lateral (visitante nuevo)."""
    client = make_client(service)
    res = client.post(BASE)
    assert res.status_code == 201
    assert res.json() == {"conversacionId": str(CID)}
    raw = client.cookies.get("chat_sid")
    assert raw and len(raw) >= 32
    set_cookie = res.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie and "secure" in set_cookie
    # only the SHA-256 of the cookie is stored as owner
    assert service.created_for.anonymous_sid == hashlib.sha256(raw.encode()).hexdigest()


def test_nueva_conversacion_secure_sigue_la_configuracion(service):
    """SPEC-05 Req. 1 · Scenario: Nueva conversación desde la barra lateral (COOKIE_SECURE=false)."""
    res = make_client(service, cookie_secure=False).post(BASE)
    assert "secure" not in res.headers["set-cookie"].lower()


def test_nueva_conversacion_con_cookie_la_reutiliza_sin_emitir_otra(service):
    """SPEC-05 Req. 1 · Scenario: Nueva conversación desde la barra lateral (visitante recurrente)."""
    client = make_client(service)
    client.cookies.set("chat_sid", "abc")
    res = client.post(BASE)
    assert res.status_code == 201
    assert "set-cookie" not in res.headers
    assert service.created_for.anonymous_sid == hashlib.sha256(b"abc").hexdigest()


def test_listado_de_conversaciones_recientes_usa_forma_del_frontend_y_pagina(service):
    """SPEC-05 Req. 1 · Scenario: Listado de conversaciones recientes."""
    client = make_client(service, identity=CallerIdentity(customer_id=uuid.uuid4()))
    res = client.get(BASE, params={"pagina": 2})
    assert res.status_code == 200
    assert res.json() == [{
        "id": str(CID), "titulo": "Zapatillas", "preview": "Talla 42",
        "ultimoMensajeEn": "2026-10-01T00:00:00Z",
    }]
    assert service.list_call[1] == 2


def test_listado_sin_cookie_es_pagina_vacia_y_pagina_por_defecto_1(service):
    """SPEC-05 Req. 1 · Scenario: Listado de conversaciones recientes (visitante sin cookie)."""
    res = make_client(service).get(BASE)
    assert res.status_code == 200 and res.json() == []
    assert service.list_call == (None, 1)


def test_listado_pagina_invalida_es_validacion(service):
    """SPEC-05 Req. 1 · Scenario: Listado de conversaciones recientes (pagina < 1)."""
    res = make_client(service).get(BASE, params={"pagina": 0})
    assert res.status_code == 400
    assert res.headers["content-type"].startswith("application/problem+json")
    assert res.json()["code"] == "VALIDACION"


def test_retomar_una_conversacion_devuelve_historial_con_roles_del_frontend(service):
    """SPEC-05 Req. 1 · Scenario: Retomar una conversación (propia)."""
    client = make_client(service, identity=CallerIdentity(anonymous_sid="h"))
    res = client.get(f"{BASE}/{CID}/mensajes")
    assert res.status_code == 200
    body = res.json()
    assert [(m["rol"], m["texto"]) for m in body] == [("user", "hola"), ("bot", "buenas")]
    assert body[0]["conversacionId"] == str(CID) and body[1]["bloques"] == {"x": 1}


@pytest.mark.parametrize("target", [str(uuid.uuid4())])
def test_retomar_conversacion_ajena_o_inexistente_es_404_problem_json(service, target):
    """SPEC-05 Req. 1 · Scenario: Retomar una conversación (ajena o inexistente)."""
    client = make_client(service, identity=CallerIdentity(anonymous_sid="h"))
    res = client.get(f"{BASE}/{target}/mensajes")
    assert res.status_code == 404
    assert res.headers["content-type"].startswith("application/problem+json")
    assert res.json()["code"] == "RECURSO_NO_ENCONTRADO"


def test_retomar_sin_cookie_es_404(service):
    """SPEC-05 Req. 1 · Scenario: Retomar una conversación (sin cookie)."""
    res = make_client(service).get(f"{BASE}/{CID}/mensajes")
    assert res.status_code == 404 and res.json()["code"] == "RECURSO_NO_ENCONTRADO"


@pytest.mark.parametrize("method,path", [("post", ""), ("get", ""), ("get", f"/{CID}/mensajes")])
def test_authorization_header_responde_501_sin_confiar_en_el_token(service, method, path):
    """Provisional: JWT validation belongs to H3-09; never trust an unverified token."""
    client = make_client(service)
    res = getattr(client, method)(BASE + path, headers={"Authorization": "Bearer x.y.z"})
    assert res.status_code == 501
    assert res.headers["content-type"].startswith("application/problem+json")
    assert res.json()["code"] == "AUTENTICACION_NO_DISPONIBLE"
    assert service.created_for is None and service.list_call is None


def test_health_responde_ok_sin_base_de_datos():
    """Health endpoint: no database access."""
    assert TestClient(create_app()).get("/health").json() == {"status": "ok"}


def test_health_router_aislado():
    app = FastAPI()
    app.include_router(health_router)
    assert TestClient(app).get("/health").status_code == 200


def test_buscar_devuelve_forma_del_listado_recorta_q_y_pagina(service):
    """SPEC-05 Req. 1 · Scenario: Buscar chats."""
    client = make_client(service, identity=CallerIdentity(customer_id=uuid.uuid4()))
    res = client.get(f"{BASE}/buscar", params={"q": "  zapatillas ", "pagina": 2})
    assert res.status_code == 200
    assert res.json() == [{
        "id": str(CID), "titulo": "Zapatillas", "preview": "Talla 42",
        "ultimoMensajeEn": "2026-10-01T00:00:00Z",
    }]
    assert service.search_call[1:] == ("zapatillas", 2)


def test_buscar_no_es_capturada_por_las_rutas_con_id(service):
    """SPEC-05 Req. 1 · Scenario: Buscar chats (`buscar` is not parsed as a conversation id)."""
    res = make_client(service, identity=CallerIdentity(customer_id=uuid.uuid4())).get(f"{BASE}/buscar?q=a")
    assert res.status_code == 200 and service.search_call is not None


def test_buscar_sin_cookie_es_lista_vacia(service):
    """SPEC-05 Req. 1 · Scenario: Buscar chats (visitante sin cookie)."""
    res = make_client(service).get(f"{BASE}/buscar", params={"q": "zapatillas"})
    assert res.status_code == 200 and res.json() == []
    assert service.search_call == (None, "zapatillas", 1)


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "   "}, {"q": "x" * 101}, {"q": "a", "pagina": 0}])
def test_buscar_consulta_invalida_es_validacion(service, params):
    """SPEC-05 Req. 1 · Scenario: Buscar chats (q missing, blank, too long or bad page)."""
    res = make_client(service, identity=CallerIdentity(customer_id=uuid.uuid4())).get(
        f"{BASE}/buscar", params=params
    )
    assert res.status_code == 400
    assert res.headers["content-type"].startswith("application/problem+json")
    assert res.json()["code"] == "VALIDACION"
    assert service.search_call is None


def test_buscar_acepta_q_de_100_caracteres(service):
    """SPEC-05 Req. 1 · Scenario: Buscar chats (length boundary)."""
    res = make_client(service, identity=CallerIdentity(customer_id=uuid.uuid4())).get(
        f"{BASE}/buscar", params={"q": "x" * 100}
    )
    assert res.status_code == 200
