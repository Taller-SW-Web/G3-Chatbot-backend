"""Application service for G5 Postventa (F3, F5, F6).

Owns the channel rules that G5 delegates to the caller:
- customer-token strategy for claims/returns/CSAT;
- order ownership check for claims (G5 only validates the id format);
- local idempotency via `claim_ref` / `return_ref`;
- G3-facing pagination and response normalization.
"""
import uuid
from typing import Any

from src.application.dto.postventa_dto import (
    PostventaError,
    to_g5_claim_reason,
    to_g5_evidence_type,
    to_g5_return_reason,
    to_g5_return_type,
)
from src.ports.outbound.postventa_port import PostventaPort
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
    ) -> None:
        self._postventa = postventa
        self._sales = sales
        self._claim_refs = claim_refs
        self._return_refs = return_refs
        self._evidence = evidence

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
            from src.shared.infrastructure.db.models.evidence import Evidence

            row = Evidence(
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
        result = await self._postventa.create_return(
            normalized, holder_token=holder_token, correlation_id=correlation_id
        )
        if self._return_refs is not None:
            from src.shared.infrastructure.db.models.return_ref import ReturnRef

            await self._return_refs.guardar(
                ReturnRef(
                    return_id=str(result["devolucionId"]),
                    code=str(result["devolucionId"]),
                    order_id=str(payload["pedidoId"]),
                    customer_id=customer_id,
                    requested_type=str(payload["tipo"]),
                    idempotency_key=idempotency_key,
                )
            )
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
        result = await self._postventa.create_claim(
            normalized, holder_token=holder_token, correlation_id=correlation_id
        )
        if self._claim_refs is not None:
            from src.shared.infrastructure.db.models.claim_ref import ClaimRef

            await self._claim_refs.guardar(
                ClaimRef(
                    claim_id=str(result["reclamoId"]),
                    code=str(result["codigoSeguimiento"]),
                    order_id=str(order_id or ""),
                    customer_id=customer_id,
                    idempotency_key=idempotency_key,
                )
            )
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
        return await self._postventa.create_csat(
            g5_payload, holder_token=holder_token, correlation_id=correlation_id
        )

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
