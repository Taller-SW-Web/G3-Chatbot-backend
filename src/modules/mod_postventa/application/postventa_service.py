"""Application service for G5 Postventa (F3, F5, F6).

Owns the channel rules that G5 delegates to the caller:
- customer-token strategy for claims/returns/CSAT;
- order ownership check for claims (G5 only validates the id format);
- local idempotency via `claim_ref` / `return_ref`;
- G3-facing pagination and response normalization.
"""
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from src.modules.mod_postventa.application.postventa_dto import (
    PostventaError,
    to_g5_claim_reason,
    to_g5_evidence_type,
    to_g5_return_reason,
    to_g5_return_type,
)
from src.modules.mod_postventa.domain.references import (
    NewClaimReference,
    NewEvidenceReference,
    NewReturnReference,
)
from src.modules.mod_postventa.ports.postventa_port import PostventaPort
from src.shared.application.ports.conversation_repository_port import ConversationRepositoryPort
from src.shared.application.ports.order_repository_port import SalesOrderPort
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.module_errors import ModuleRequestError
from src.shared.domain.request_context import correlation_id_var

logger = logging.getLogger(__name__)

# SPEC-21 Req. 5 / SPEC-19 Req. 3: list until exhausted; 50 was the old fixed first page.
DUPLICATE_PAGE_SIZE = 50
# SPEC-19/21: a rejected draft stays readable for 24 h, then it is ignored and purged.
DRAFT_TTL = timedelta(hours=24)
RETURN_DRAFT_CONTEXT_KEY = "postventaReturnDraft"
CLAIM_DRAFT_CONTEXT_KEY = "postventaClaimDraft"


class PostventaService:
    def __init__(
        self,
        postventa: PostventaPort,
        sales: SalesOrderPort,
        claim_refs: Any | None = None,
        return_refs: Any | None = None,
        evidence: Any | None = None,
        order_refs: Any | None = None,
        conversations: ConversationRepositoryPort | None = None,
        uow: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._postventa = postventa
        self._sales = sales
        self._claim_refs = claim_refs
        self._return_refs = return_refs
        self._evidence = evidence
        self._order_refs = order_refs
        self._conversations = conversations
        self._uow = uow
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def upload_evidence(
        self,
        *,
        holder_token: str,
        file_name: str,
        content_type: str,
        content: bytes,
        conversation_id: uuid.UUID | None = None,
        customer_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        # Ownership first: never upload to G5 for a foreign or missing conversation.
        if conversation_id is not None and self._conversations is not None:
            owned = (
                await self._conversations.find_owned(
                    conversation_id, CallerIdentity(customer_id=customer_id)
                )
                if customer_id is not None
                else None
            )
            if owned is None:
                raise PostventaError(
                    "RECURSO_NO_ENCONTRADO", "No encontré esa conversación en tu cuenta."
                )
        result = await self._postventa.upload_evidence(
            file_name=file_name,
            content_type=content_type,
            content=content,
            holder_token=holder_token,
            correlation_id=correlation_id,
        )
        if conversation_id and self._evidence is not None:
            row = NewEvidenceReference(
                conversation_id=conversation_id,
                type=str(result.get("tipo") or "IMAGEN"),
                url=str(result["url"]),
                original_file_name=file_name,
                size_bytes=int(result.get("tamanioBytes") or len(content)),
            )
            await self._evidence.guardar(row)
            await self._commit()
        return result

    async def create_return(
        self,
        *,
        customer_id: uuid.UUID,
        holder_token: str,
        payload: dict[str, Any],
        idempotency_key: str,
        conversation_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        existing = await self._existing_return(idempotency_key)
        if existing is not None:
            return await self._postventa.get_customer_return(
                existing.return_id, holder_token, correlation_id=correlation_id
            )

        normalized = dict(payload)
        normalized["tipo"] = to_g5_return_type(str(normalized.get("tipo") or ""))
        normalized["motivo"] = to_g5_return_reason(str(normalized.get("motivo") or ""))
        normalized["evidencias"] = [
            {**item, "tipo": to_g5_evidence_type(str(item.get("tipo") or ""))}
            for item in normalized.get("evidencias", [])
        ]
        forzar = bool(normalized.pop("forzar", False))
        if conversation_id is not None:
            # Commit the draft on its own so a G5 failure keeps it recoverable.
            await self.save_return_draft(
                conversation_id=conversation_id, customer_id=customer_id, payload=payload
            )
        if not forzar:
            duplicate = await self._duplicate_return(
                holder_token,
                str(payload.get("pedidoId") or ""),
                normalized,
                correlation_id=correlation_id,
            )
            if duplicate is not None:
                raise PostventaError(
                    "DEVOLUCION_DUPLICADA",
                    "Ya existe una solicitud abierta para este pedido, motivo y línea.",
                    extra=duplicate,
                )
        result = await self._postventa.create_return(
            normalized, holder_token=holder_token, correlation_id=correlation_id
        )
        return_id = str(result["devolucionId"])
        if self._return_refs is not None:
            await self._return_refs.guardar(
                NewReturnReference(
                    return_id=return_id,
                    code=return_id,
                    order_id=str(payload["pedidoId"]),
                    customer_id=customer_id,
                    requested_type=str(payload["tipo"]),
                    idempotency_key=idempotency_key,
                )
            )
        # Same unit of work as `return_ref`: evidence rows and the draft cleanup commit together.
        await self._associate_return_evidence(
            return_id=return_id, customer_id=customer_id, normalized=normalized
        )
        if conversation_id is not None:
            await self._remove_draft(
                RETURN_DRAFT_CONTEXT_KEY, conversation_id=conversation_id, customer_id=customer_id
            )
        if self._return_refs is not None or self._evidence is not None or conversation_id is not None:
            await self._commit()
        return await self._postventa.get_customer_return(
            return_id, holder_token, correlation_id=correlation_id
        )

    async def list_returns(
        self,
        *,
        holder_token: str,
        estado: str | None = None,
        tipo: str | None = None,
        page: int = 1,
        page_size: int = 10,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        raw = await self._postventa.list_customer_returns(
            holder_token,
            estado=estado,
            tipo=tipo,
            page=page,
            page_size=page_size,
            correlation_id=correlation_id,
        )
        return self._normalize_page(raw, page, page_size)

    async def get_return(
        self,
        *,
        return_id: str,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._postventa.get_customer_return(
            return_id, holder_token, correlation_id=correlation_id
        )

    async def create_claim(
        self,
        *,
        customer_id: uuid.UUID,
        holder_token: str,
        payload: dict[str, Any],
        idempotency_key: str,
        conversation_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        existing = await self._existing_claim(idempotency_key)
        if existing is not None:
            return await self._postventa.get_claim(
                existing.code, holder_token, correlation_id=correlation_id
            )

        order_id = payload.get("pedidoId")
        if order_id:
            try:
                await self._sales.get_customer_order(
                    str(order_id), holder_token, correlation_id=correlation_id
                )
            except ModuleRequestError as exc:
                if exc.status in (403, 404):
                    raise PostventaError(
                        "RECURSO_NO_ENCONTRADO",
                        "No encontré ese pedido en tu cuenta.",
                    ) from exc
                raise
            except Exception as exc:
                raise PostventaError(
                    "SERVICIO_NO_DISPONIBLE",
                    "No puedo validar tus pedidos en este momento.",
                ) from exc

        normalized = dict(payload)
        try:
            normalized["motivo"] = to_g5_claim_reason(str(normalized.get("motivo") or ""))
        except ValueError as exc:
            raise PostventaError(
                "VALIDACION", "El motivo del reclamo no es uno de los cinco valores de G5."
            ) from exc
        # G5 requires the channel and uses it for the Libro de Reclamaciones.
        normalized["canal"] = "CHATBOT"
        # G5 has no `solucionEsperada` field: the UI appends it to `detalle`.
        normalized.pop("solucionEsperada", None)
        forzar = bool(normalized.pop("forzar", False))
        if conversation_id is not None:
            # Commit the draft on its own so a G5 failure keeps it recoverable.
            await self.save_claim_draft(
                conversation_id=conversation_id, customer_id=customer_id, payload=payload
            )
        if not forzar and order_id:
            duplicate = await self._duplicate_claim(
                holder_token, str(order_id), normalized, correlation_id=correlation_id
            )
            if duplicate is not None:
                raise PostventaError(
                    "RECLAMO_DUPLICADO",
                    "Ya existe un reclamo abierto para este pedido y motivo.",
                    extra=duplicate,
                )
        result = await self._postventa.create_claim(
            normalized, holder_token=holder_token, correlation_id=correlation_id
        )
        if self._claim_refs is not None:
            await self._claim_refs.guardar(
                NewClaimReference(
                    claim_id=str(result["reclamoId"]),
                    code=str(result["codigoSeguimiento"]),
                    order_id=str(order_id or ""),
                    customer_id=customer_id,
                    idempotency_key=idempotency_key,
                )
            )
        if conversation_id is not None:
            await self._remove_draft(
                CLAIM_DRAFT_CONTEXT_KEY, conversation_id=conversation_id, customer_id=customer_id
            )
        if self._claim_refs is not None or conversation_id is not None:
            await self._commit()
        return await self._postventa.get_claim(
            str(result["codigoSeguimiento"]), holder_token, correlation_id=correlation_id
        )

    async def list_claims(
        self,
        *,
        holder_token: str,
        estado: str | None = None,
        page: int = 1,
        page_size: int = 10,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        raw = await self._postventa.list_customer_claims(
            holder_token,
            estado=estado,
            page=page,
            page_size=page_size,
            correlation_id=correlation_id,
        )
        return self._normalize_page(raw, page, page_size)

    async def get_claim(
        self,
        *,
        code: str,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._postventa.get_claim(code, holder_token, correlation_id=correlation_id)

    async def create_csat(
        self,
        *,
        customer_id: uuid.UUID,
        holder_token: str,
        order_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        if payload.get("puntuacion") not in (1, 3, 5):
            raise PostventaError(
                "VALIDACION", "La puntuación del CSAT debe ser 1, 3 o 5."
            )
        try:
            order = await self._sales.get_customer_order(
                order_id, holder_token, correlation_id=correlation_id
            )
        except ModuleRequestError as exc:
            if exc.status in (403, 404):
                raise PostventaError("RECURSO_NO_ENCONTRADO", "No encontré ese pedido en tu cuenta.") from exc
            raise
        if str(order.get("estado") or "").upper() != "ENTREGADO":
            raise PostventaError(
                "ORDER_NOT_DELIVERED",
                "Solo puedes calificar un pedido entregado.",
            )
        g5_payload = {
            "pedidoId": order_id,
            "clienteId": str(customer_id),
            "canal": "CHATBOT",
            "puntuacion": payload["puntuacion"],
            "comentario": payload.get("comentario"),
        }
        try:
            answer = await self._postventa.create_csat(
                g5_payload, holder_token=holder_token, correlation_id=correlation_id
            )
        except ModuleRequestError as exc:
            if exc.code == "CSAT_ALREADY_SUBMITTED":
                await self._mark_csat_sent(order_id)
            raise
        await self._mark_csat_sent(order_id)
        return answer

    async def _mark_csat_sent(self, order_id: str) -> None:
        if self._order_refs is None:
            return
        try:
            await self._order_refs.marcar_csat_enviado(order_id)
            await self._commit()
        except Exception:
            # The survey answer is already authoritative in Ventas; a local flag is best effort.
            pass

    async def _commit(self) -> None:
        if self._uow is not None:
            await self._uow.commit()

    # -- drafts in `conversation.context` (SPEC-19/21, 24 h retention) -------------

    async def save_return_draft(
        self,
        *,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> None:
        """Persist the return draft so a later request or a G5 failure can resume it."""
        await self._save_draft(
            RETURN_DRAFT_CONTEXT_KEY,
            conversation_id=conversation_id,
            customer_id=customer_id,
            payload=payload,
        )

    async def get_return_draft(
        self, *, conversation_id: uuid.UUID, customer_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """Draft payload, or None when missing or older than 24 h (SPEC-21 retention)."""
        return await self._load_draft(
            RETURN_DRAFT_CONTEXT_KEY,
            conversation_id=conversation_id,
            customer_id=customer_id,
        )

    async def clear_return_draft(
        self, *, conversation_id: uuid.UUID, customer_id: uuid.UUID
    ) -> None:
        await self._remove_draft(
            RETURN_DRAFT_CONTEXT_KEY,
            conversation_id=conversation_id,
            customer_id=customer_id,
        )
        await self._commit()

    async def save_claim_draft(
        self,
        *,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> None:
        """Persist the claim draft so a later request or a G5 failure can resume it."""
        await self._save_draft(
            CLAIM_DRAFT_CONTEXT_KEY,
            conversation_id=conversation_id,
            customer_id=customer_id,
            payload=payload,
        )

    async def get_claim_draft(
        self, *, conversation_id: uuid.UUID, customer_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """Draft payload, or None when missing or older than 24 h (SPEC-19 retention)."""
        return await self._load_draft(
            CLAIM_DRAFT_CONTEXT_KEY,
            conversation_id=conversation_id,
            customer_id=customer_id,
        )

    async def clear_claim_draft(
        self, *, conversation_id: uuid.UUID, customer_id: uuid.UUID
    ) -> None:
        await self._remove_draft(
            CLAIM_DRAFT_CONTEXT_KEY,
            conversation_id=conversation_id,
            customer_id=customer_id,
        )
        await self._commit()

    async def _save_draft(
        self,
        key: str,
        *,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> None:
        if self._conversations is None:
            return
        await self._require_owned_conversation(conversation_id, customer_id)
        context = await self._conversations.get_context_for_update(conversation_id)
        context[key] = {
            "payload": dict(payload),
            "savedAt": self._clock().astimezone(timezone.utc).isoformat(),
        }
        await self._conversations.save_context(conversation_id, context)
        # The draft must survive the G5 call that follows; commit it on its own.
        await self._commit()

    async def _load_draft(
        self, key: str, *, conversation_id: uuid.UUID, customer_id: uuid.UUID
    ) -> dict[str, Any] | None:
        if self._conversations is None:
            return None
        await self._require_owned_conversation(conversation_id, customer_id)
        context = await self._conversations.get_context(conversation_id)
        entry = context.get(key)
        if not isinstance(entry, dict):
            return None
        payload = entry.get("payload")
        saved_at = self._parse_saved_at(entry.get("savedAt"))
        if not isinstance(payload, dict) or saved_at is None:
            return None
        if self._clock() - saved_at > DRAFT_TTL:
            return None
        return dict(payload)

    async def _remove_draft(
        self, key: str, *, conversation_id: uuid.UUID, customer_id: uuid.UUID
    ) -> None:
        if self._conversations is None:
            return
        await self._require_owned_conversation(conversation_id, customer_id)
        context = await self._conversations.get_context_for_update(conversation_id)
        if key not in context:
            return
        context.pop(key)
        await self._conversations.save_context(conversation_id, context)

    async def _require_owned_conversation(
        self, conversation_id: uuid.UUID, customer_id: uuid.UUID
    ) -> None:
        if self._conversations is None:
            return
        owned = await self._conversations.find_owned(
            conversation_id, CallerIdentity(customer_id=customer_id)
        )
        if owned is None:
            raise PostventaError(
                "RECURSO_NO_ENCONTRADO", "No encontré esa conversación en tu cuenta."
            )

    @staticmethod
    def _parse_saved_at(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    # -- duplicate detection -------------------------------------------------------

    async def _duplicate_claim(
        self,
        holder_token: str,
        order_id: str,
        normalized: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """SPEC-19 Req. 3: one open claim per order + reason, filtered locally."""
        reason = str(normalized.get("motivo") or "")
        try:
            pages = self._pages(
                lambda page: self._postventa.list_customer_claims(
                    holder_token,
                    page=page,
                    page_size=DUPLICATE_PAGE_SIZE,
                    correlation_id=correlation_id,
                )
            )
            async for items in pages:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    estado = str(item.get("estado") or "").upper()
                    if estado in {"ATENDIDO", "DERIVADO"}:
                        continue
                    if str(item.get("pedidoId") or "") != order_id:
                        continue
                    if str(item.get("motivo") or "") != reason:
                        continue
                    return {
                        "reclamoId": item.get("reclamoId"),
                        "codigoSeguimiento": item.get("codigoSeguimiento"),
                        "estado": estado,
                    }
        except Exception as exc:
            # SPEC-19 Req. 4: the claim is the main operation; do not block on the check.
            self._warn_duplicate_failure("reclamos", correlation_id, exc)
        return None

    async def _duplicate_return(
        self,
        holder_token: str,
        order_id: str,
        normalized: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """SPEC-21 Req. 5: open return for the same order, reason and lines."""
        reason = str(normalized.get("motivo") or "")
        requested = self._lines_signature(normalized.get("items") or [])
        try:
            pages = self._pages(
                lambda page: self._postventa.list_customer_returns(
                    holder_token,
                    page=page,
                    page_size=DUPLICATE_PAGE_SIZE,
                    correlation_id=correlation_id,
                )
            )
            async for items in pages:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    estado = str(item.get("estado") or "").upper()
                    if estado in {"RECHAZADA", "COMPLETADA"}:
                        continue
                    if str(item.get("pedidoId") or "") != order_id:
                        continue
                    if str(item.get("motivo") or "") != reason:
                        continue
                    return_id = item.get("devolucionId")
                    if not return_id:
                        continue
                    try:
                        detail = await self._postventa.get_customer_return(
                            str(return_id), holder_token, correlation_id=correlation_id
                        )
                    except Exception:
                        # Without the detail the lines cannot be compared; do not block.
                        continue
                    if self._lines_signature(detail.get("items") or []) == requested:
                        return {
                            "devolucionId": return_id,
                            "estado": estado,
                            "motivo": reason,
                        }
        except Exception as exc:
            # SPEC-21 Req. 5: fail-open; a previous page without a match is not proof.
            self._warn_duplicate_failure("devoluciones", correlation_id, exc)
        return None

    async def _pages(
        self, fetch: Callable[[int], Awaitable[dict[str, Any]]]
    ) -> AsyncIterator[list[Any]]:
        """Yield all list pages; page 51 is reachable (the old fixed page silently missed it)."""
        page = 1
        while True:
            raw = await fetch(page)
            items = raw.get("contenido") or raw.get("items") or []
            if not isinstance(items, list):
                items = []
            yield items
            if len(items) < DUPLICATE_PAGE_SIZE:
                return
            total = raw.get("totalElementos", raw.get("total"))
            try:
                if total is not None and page * DUPLICATE_PAGE_SIZE >= int(total):
                    return
            except (TypeError, ValueError):
                pass
            page += 1

    @staticmethod
    def _warn_duplicate_failure(
        kind: str, correlation_id: str | None, exc: Exception
    ) -> None:
        logger.warning(
            "No se pudo consultar el listado de %s; se continúa sin bloquear (fail-open) "
            "[correlation_id=%s]: %s",
            kind,
            correlation_id or correlation_id_var.get(),
            type(exc).__name__,
        )

    # -- evidence linkage ----------------------------------------------------------

    async def _associate_return_evidence(
        self,
        *,
        return_id: str,
        customer_id: uuid.UUID,
        normalized: dict[str, Any],
    ) -> None:
        """Attach the uploaded drafts of this request to the new return, same UoW."""
        if self._evidence is None:
            return
        listar = getattr(self._evidence, "listar_borrador_por_urls", None)
        if listar is None:
            return
        urls = sorted(
            {
                str(item["url"])
                for item in normalized.get("evidencias") or []
                if isinstance(item, dict) and item.get("url")
            }
        )
        if not urls:
            return
        rows = await listar(urls, customer_id)
        for row in rows:
            await self._evidence.asociar_devolucion(row.id, row.conversation_id, return_id)

    @staticmethod
    def _lines_signature(items: list[Any]) -> tuple[tuple[str, int], ...]:
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            rows.append((str(item.get("productoId") or ""), int(item.get("cantidad") or 0)))
        return tuple(sorted(rows))

    async def _existing_return(self, idempotency_key: str) -> Any | None:
        if not idempotency_key or self._return_refs is None:
            return None
        return await self._return_refs.obtener_por_idempotency_key(idempotency_key)

    async def _existing_claim(self, idempotency_key: str) -> Any | None:
        if not idempotency_key or self._claim_refs is None:
            return None
        return await self._claim_refs.obtener_por_idempotency_key(idempotency_key)

    @staticmethod
    def _normalize_page(raw: dict[str, Any], page: int, page_size: int) -> dict[str, Any]:
        items = raw.get("contenido") or raw.get("items") or []
        total = raw.get("totalElementos", raw.get("total", len(items)))
        return {
            "items": items,
            "total": int(total),
            "page": page,
            "pageSize": page_size,
        }
