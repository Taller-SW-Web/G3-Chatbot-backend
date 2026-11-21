"""Application service for G5 Postventa (F3, F5, F6).

Owns the channel rules that G5 delegates to the caller:
- customer-token strategy for claims/returns/CSAT;
- order ownership check for claims (G5 only validates the id format);
- local idempotency via `claim_ref` / `return_ref`;
- G3-facing pagination and response normalization.
"""
import uuid
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
from src.shared.application.ports.order_repository_port import SalesOrderPort
from src.shared.domain.module_errors import ModuleRequestError


class PostventaService:
    def __init__(
        self,
        postventa: PostventaPort,
        sales: SalesOrderPort,
        claim_refs: Any | None = None,
        return_refs: Any | None = None,
        evidence: Any | None = None,
        order_refs: Any | None = None,
        uow: Any | None = None,
    ) -> None:
        self._postventa = postventa
        self._sales = sales
        self._claim_refs = claim_refs
        self._return_refs = return_refs
        self._evidence = evidence
        self._order_refs = order_refs
        self._uow = uow

    async def upload_evidence(
        self,
        *,
        holder_token: str,
        file_name: str,
        content_type: str,
        content: bytes,
        conversation_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
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
        return result

    async def create_return(
        self,
        *,
        customer_id: uuid.UUID,
        holder_token: str,
        payload: dict[str, Any],
        idempotency_key: str,
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
        if not forzar:
            duplicate = await self._duplicate_return(
                holder_token, str(payload.get("pedidoId") or ""), normalized
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
        if self._return_refs is not None:
            await self._return_refs.guardar(
                NewReturnReference(
                    return_id=str(result["devolucionId"]),
                    code=str(result["devolucionId"]),
                    order_id=str(payload["pedidoId"]),
                    customer_id=customer_id,
                    requested_type=str(payload["tipo"]),
                    idempotency_key=idempotency_key,
                )
            )
            await self._commit()
        return await self._postventa.get_customer_return(
            str(result["devolucionId"]), holder_token, correlation_id=correlation_id
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
        normalized["motivo"] = to_g5_claim_reason(str(normalized.get("motivo") or ""))
        # G5 requires the channel and uses it for the Libro de Reclamaciones.
        normalized["canal"] = "CHATBOT"
        # G5 has no `solucionEsperada` field: the UI appends it to `detalle`.
        normalized.pop("solucionEsperada", None)
        forzar = bool(normalized.pop("forzar", False))
        if not forzar and order_id:
            duplicate = await self._duplicate_claim(holder_token, str(order_id), normalized)
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

    async def _duplicate_claim(
        self, holder_token: str, order_id: str, normalized: dict[str, Any]
    ) -> dict[str, Any] | None:
        """SPEC-19 Req. 3: one open claim per order + reason, filtered locally."""
        raw = await self._postventa.list_customer_claims(
            holder_token, page=1, page_size=50
        )
        reason = str(normalized.get("motivo") or "")
        for item in raw.get("contenido") or raw.get("items") or []:
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
        return None

    async def _duplicate_return(
        self, holder_token: str, order_id: str, normalized: dict[str, Any]
    ) -> dict[str, Any] | None:
        """SPEC-21 Req. 5: open return for the same order, reason and lines."""
        raw = await self._postventa.list_customer_returns(
            holder_token, page=1, page_size=50
        )
        reason = str(normalized.get("motivo") or "")
        requested = self._lines_signature(normalized.get("items") or [])
        for item in raw.get("contenido") or raw.get("items") or []:
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
                detail = await self._postventa.get_customer_return(str(return_id), holder_token)
            except Exception:
                # Without the detail the lines cannot be compared; do not block.
                continue
            if self._lines_signature(detail.get("items") or []) == requested:
                return {
                    "devolucionId": return_id,
                    "estado": estado,
                    "motivo": reason,
                }
        return None

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
