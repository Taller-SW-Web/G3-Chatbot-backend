"""FeedbackService: reactions and suggestions/bugs (SPEC-24)."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.modules.mod_chat.application.feedback_service import FeedbackError, FeedbackService
from src.modules.mod_chat.ports.image_processor_port import ProcessedImage
from src.shared.application.ports.attachment_storage_port import SignedUrl
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.entities.conversation import Conversation
from src.shared.domain.entities.message import Message, MessageRole

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
OWNER = CallerIdentity(customer_id=uuid.uuid4())
OTHER = CallerIdentity(customer_id=uuid.uuid4())
CONVERSATION = uuid.uuid4()
MESSAGE = uuid.uuid4()


class FakeMessages:
    async def obtener_por_id(self, message_id):
        if message_id != MESSAGE:
            return None
        return Message(
            id=MESSAGE, conversation_id=CONVERSATION, role=MessageRole.ASSISTANT,
            text="hola", blocks=None, created_at=NOW,
        )


class FakeConversations:
    async def find_owned(self, conversation_id, owner):
        if conversation_id == CONVERSATION and owner == OWNER:
            return Conversation(id=CONVERSATION, title="t", last_message_at=NOW, summary=None)
        return None


class FakeReactions:
    def __init__(self):
        self.rows = {}

    async def upsert(self, message_id, *, customer_id, anonymous_sid, reaction):
        self.rows[(message_id, customer_id, anonymous_sid)] = reaction
        return SimpleNamespace(reaction=reaction)


class FakeSuggestions:
    def __init__(self):
        self.rows = {}
        self.status = {}

    async def obtener_por_idempotency_key(self, key):
        return self.rows.get(key)

    async def guardar(self, row):
        saved = SimpleNamespace(
            id=uuid.uuid4(), customer_id=row.customer_id, type=row.type, title=row.title,
            description=row.description, contact_email=row.contact_email,
            screenshot_key=row.screenshot_key, idempotency_key=row.idempotency_key,
            notification_status="PENDING",
        )
        self.rows[row.idempotency_key] = saved
        return saved

    async def marcar_notificacion(self, suggestion_id, status):
        self.status[suggestion_id] = status


class FakeOutbox:
    def __init__(self):
        self.jobs = []

    async def guardar(self, job):
        self.jobs.append(job)
        return job


class FakeUow:
    async def commit(self):
        pass

    async def rollback(self):
        pass


class FakeStorage:
    def __init__(self):
        self.files = {}

    async def guardar(self, key, data, content_type):
        self.files[key] = data

    async def eliminar(self, key):
        self.files.pop(key, None)

    async def leer(self, key):
        return self.files[key]

    def url_firmada(self, key, ttl_seconds):
        return SignedUrl("http://x", NOW)

    def verificar_firma(self, key, exp, sig):
        return True

    async def listar_claves(self):
        return list(self.files)


class FakeProcessor:
    def __init__(self, mime="image/png"):
        self.mime = mime

    def procesar(self, data):
        return ProcessedImage(self.mime, b"normalized", b"thumb", 100, 100)


class FakeNotifier:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send(self, payload):
        if self.fail:
            raise TimeoutError("mail down")
        self.sent.append(payload)


def build(*, notifier=None, processor=None):
    reactions, suggestions, outbox = FakeReactions(), FakeSuggestions(), FakeOutbox()
    service = FeedbackService(
        reactions, FakeMessages(), FakeConversations(), suggestions, outbox, FakeUow(),
        storage=FakeStorage(), processor=processor or FakeProcessor(), notifier=notifier,
        send_inline=True,
    )
    return service, reactions, suggestions, outbox


@pytest.mark.asyncio
async def test_reaction_is_upserted_for_the_message_owner():
    service, reactions, _, _ = build()
    answer = await service.react(OWNER, MESSAGE, "POSITIVE")
    assert answer["reaccion"] == "POSITIVE"
    assert reactions.rows[(MESSAGE, OWNER.customer_id, None)] == "POSITIVE"


@pytest.mark.asyncio
async def test_reaction_on_a_foreign_message_is_404():
    service, _, _, _ = build()
    with pytest.raises(FeedbackError) as exc:
        await service.react(OTHER, MESSAGE, "NEGATIVE")
    assert exc.value.code == "RECURSO_NO_ENCONTRADO" and exc.value.status == 404


@pytest.mark.asyncio
async def test_suggestion_is_registered_and_queued_for_internal_email():
    notifier = FakeNotifier()
    service, _, suggestions, outbox = build(notifier=notifier)
    answer = await service.create_suggestion(
        OWNER.customer_id,
        kind="Bug", title="No carga el carrito", description="Pasa al abrir el carrito.",
        contact_email="ana@example.com", idempotency_key="sug-1",
    )
    assert answer["codigoSeguimiento"].startswith("SUG-")
    assert outbox.jobs[0].type == "SEND_FEEDBACK_EMAIL"
    assert notifier.sent[0]["titulo"] == "No carga el carrito"
    assert suggestions.status[uuid.UUID(outbox.jobs[0].payload["suggestion_id"])] == "SENT"


@pytest.mark.asyncio
async def test_same_idempotency_key_returns_the_same_suggestion():
    service, _, _, outbox = build()
    first = await service.create_suggestion(
        OWNER.customer_id, kind="SUGGESTION", title="Idea", description="Una idea útil.",
        contact_email="ana@example.com", idempotency_key="same-key",
    )
    second = await service.create_suggestion(
        OWNER.customer_id, kind="SUGGESTION", title="Idea", description="Una idea útil.",
        contact_email="ana@example.com", idempotency_key="same-key",
    )
    assert first == second
    assert len(outbox.jobs) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "BUG", "title": "ab", "description": "Suficiente texto.", "contact_email": "a@b.com"},
        {"kind": "BUG", "title": "Título", "description": "corto", "contact_email": "a@b.com"},
        {"kind": "OTRO", "title": "Título", "description": "Suficiente texto.", "contact_email": "a@b.com"},
        {"kind": "BUG", "title": "Título", "description": "Suficiente texto.", "contact_email": "malo"},
    ],
)
@pytest.mark.asyncio
async def test_invalid_suggestion_fields_are_400(payload):
    service, _, _, _ = build()
    with pytest.raises(FeedbackError) as exc:
        await service.create_suggestion(
            OWNER.customer_id, idempotency_key="key", **payload
        )
    assert exc.value.code == "VALIDACION" and exc.value.status == 400


@pytest.mark.asyncio
async def test_invalid_screenshot_is_400_before_saving():
    service, _, suggestions, _ = build(processor=FakeProcessor(mime="image/webp"))
    with pytest.raises(FeedbackError) as exc:
        await service.create_suggestion(
            OWNER.customer_id, kind="BUG", title="Título", description="Suficiente texto.",
            contact_email="ana@example.com", idempotency_key="shot", screenshot=b"webp",
        )
    assert exc.value.code == "ADJUNTO_INVALIDO"
    assert suggestions.rows == {}


@pytest.mark.asyncio
async def test_notifier_failure_does_not_lose_the_suggestion():
    notifier = FakeNotifier(fail=True)
    service, _, suggestions, outbox = build(notifier=notifier)
    answer = await service.create_suggestion(
        OWNER.customer_id, kind="BUG", title="Título", description="Suficiente texto.",
        contact_email="ana@example.com", idempotency_key="mail-fail",
    )
    assert answer["sugerenciaId"]
    assert len(outbox.jobs) == 1
    assert suggestions.status == {}
