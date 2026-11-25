"""In-process G5 Postventa fake for tests and SALES_MODE=fake."""
from datetime import datetime, timezone
from typing import Any

from src.modules.mod_postventa.ports.postventa_port import PostventaPort
from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.fakes.fake_ventas import _holder_id


class FakePostventaPort(PostventaPort):
    def __init__(self) -> None:
        self.returns: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, dict[str, Any]] = {}
        self.claim_payloads: list[dict[str, Any]] = []
        self.csat: dict[str, dict[str, Any]] = {}
        self._return_seq = 0
        self._claim_seq = 0

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    async def upload_evidence(
        self,
        *,
        file_name: str,
        content_type: str,
        content: bytes,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        kind = "DOCUMENTO" if "pdf" in (content_type or "").lower() else "IMAGEN"
        return {
            "tipo": kind,
            "url": f"https://g5-fake.local/evidencias/{file_name}",
            "nombreArchivoOriginal": file_name,
            "tamanioBytes": len(content),
            "fechaSubida": self._now(),
        }

    async def create_return(
        self,
        payload: dict[str, Any],
        *,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        owner = _holder_id(holder_token)
        self._return_seq += 1
        return_id = f"DEV-2026-{self._return_seq:04d}"
        row = {
            "devolucionId": return_id,
            "pedidoId": payload["pedidoId"],
            "tipo": payload["tipo"],
            "estado": "SOLICITADA",
            "motivo": payload["motivo"],
            "descripcion": payload.get("descripcion"),
            "items": payload.get("items", []),
            "evidencias": payload.get("evidencias", []),
            "fechaRegistro": self._now(),
            "owner": owner,
            "resolucion": None,
            "historial": [],
            "recepciones": [],
        }
        self.returns[return_id] = row
        return {
            "devolucionId": return_id,
            "pedidoId": row["pedidoId"],
            "tipo": row["tipo"],
            "estado": row["estado"],
            "fechaRegistro": row["fechaRegistro"],
        }

    async def list_customer_returns(
        self,
        holder_token: str,
        *,
        estado: str | None = None,
        tipo: str | None = None,
        page: int = 1,
        page_size: int = 10,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        owner = _holder_id(holder_token)
        rows = [
            r
            for r in self.returns.values()
            if r["owner"] == owner
            and (not estado or r["estado"] == estado)
            and (not tipo or r["tipo"] == tipo)
        ]
        start = max(page - 1, 0) * page_size
        page_rows = rows[start : start + page_size]
        return {
            "contenido": [
                {
                    "devolucionId": r["devolucionId"],
                    "pedidoId": r["pedidoId"],
                    "tipo": r["tipo"],
                    "estado": r["estado"],
                    "motivo": r["motivo"],
                    "estadoReembolso": r.get("estadoReembolso"),
                    "fechaRegistro": r["fechaRegistro"],
                }
                for r in page_rows
            ],
            "pagina": max(page - 1, 0),
            "totalPaginas": max((len(rows) + page_size - 1) // page_size, 1),
            "totalElementos": len(rows),
        }

    async def get_customer_return(
        self,
        return_id: str,
        holder_token: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        row = self.returns.get(return_id)
        if not row or row["owner"] != _holder_id(holder_token):
            raise ModuleRequestError("postventa", 404, "DEVOLUCION_NO_ENCONTRADA")
        return {k: v for k, v in row.items() if k != "owner"}

    async def create_claim(
        self,
        payload: dict[str, Any],
        *,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        owner = _holder_id(holder_token)
        self.claim_payloads.append(payload)
        self._claim_seq += 1
        code = f"REC-2026-{self._claim_seq:04d}"
        row = {
            "reclamoId": code,
            "codigoSeguimiento": code,
            "pedidoId": payload.get("pedidoId"),
            "tipo": payload["tipo"],
            "canal": payload.get("canal", "CHATBOT"),
            "motivo": payload["motivo"],
            "detalle": payload["detalle"],
            "estado": "REGISTRADO",
            "plazoDiasHabiles": 15,
            "fechaLimiteSLA": self._now(),
            "fechaRegistro": self._now(),
            "fechaAtencion": None,
            "respuestaVisibleCliente": None,
            "estadoSla": "EN_PLAZO",
            "diasHabilesRestantes": 15,
            "slaCumplido": None,
            "owner": owner,
        }
        self.claims[code] = row
        return {k: v for k, v in row.items() if k != "owner"}

    async def list_customer_claims(
        self,
        holder_token: str,
        *,
        estado: str | None = None,
        page: int = 1,
        page_size: int = 10,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        owner = _holder_id(holder_token)
        rows = [
            c for c in self.claims.values() if c["owner"] == owner and (not estado or c["estado"] == estado)
        ]
        start = max(page - 1, 0) * page_size
        page_rows = rows[start : start + page_size]
        return {
            "contenido": [
                {
                    "reclamoId": c["reclamoId"],
                    "codigoSeguimiento": c["codigoSeguimiento"],
                    "pedidoId": c["pedidoId"],
                    "tipo": c["tipo"],
                    "motivo": c["motivo"],
                    "estado": c["estado"],
                    "fechaRegistro": c["fechaRegistro"],
                    "fechaLimiteSLA": c["fechaLimiteSLA"],
                    "estadoSla": c["estadoSla"],
                    "diasHabilesRestantes": c["diasHabilesRestantes"],
                }
                for c in page_rows
            ],
            "pagina": max(page - 1, 0),
            "totalPaginas": max((len(rows) + page_size - 1) // page_size, 1),
            "totalElementos": len(rows),
        }

    async def get_claim(
        self,
        code: str,
        holder_token: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        row = self.claims.get(code)
        if not row or row["owner"] != _holder_id(holder_token):
            raise ModuleRequestError("postventa", 404, "RECLAMO_NO_ENCONTRADO")
        return {k: v for k, v in row.items() if k != "owner"}

    async def create_csat(
        self,
        payload: dict[str, Any],
        *,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        order_id = payload["pedidoId"]
        if order_id in self.csat:
            raise ModuleRequestError("postventa", 409, "CSAT_ALREADY_SUBMITTED")
        self.csat[order_id] = {
            "owner": _holder_id(holder_token),
            "puntuacion": payload["puntuacion"],
            "comentario": payload.get("comentario"),
        }
        return {}
