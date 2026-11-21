"""Outbound port for G5 M2 (Postventa): F3 returns, F5 CSAT and F6 claims.

Authentication rule agreed with G5 (2026-11-17):
- Returns and evidence upload accept either the customer token or the channel
  service token. The chatbot uses the customer token so G5 validates ownership.
- Claims use the customer token: with a service token G5 stores `idCliente=null`
  and the claim would not appear in `/reclamos/me`.
- CSAT always uses the customer token.
"""
from typing import Any, Protocol


class PostventaPort(Protocol):
    async def upload_evidence(
        self,
        *,
        file_name: str,
        content_type: str,
        content: bytes,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`POST /api/v2/devoluciones/evidencias/upload` (F3 §2.1)."""
        ...

    async def create_return(
        self,
        payload: dict[str, Any],
        *,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`POST /api/v2/devoluciones` (F3 §2.2)."""
        ...

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
        """`GET /api/v2/devoluciones/me` (F3 §2.4b)."""
        ...

    async def get_customer_return(
        self,
        return_id: str,
        holder_token: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`GET /api/v2/devoluciones/{id}` (F3 §2.3)."""
        ...

    async def create_claim(
        self,
        payload: dict[str, Any],
        *,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`POST /api/v2/reclamos` (F6 §2.8)."""
        ...

    async def list_customer_claims(
        self,
        holder_token: str,
        *,
        estado: str | None = None,
        page: int = 1,
        page_size: int = 10,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`GET /api/v2/reclamos/me` (F6 §2.10b)."""
        ...

    async def get_claim(
        self,
        code: str,
        holder_token: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`GET /api/v2/reclamos/{codigoSeguimiento}` (F6 §2.9)."""
        ...

    async def create_csat(
        self,
        payload: dict[str, Any],
        *,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`POST /api/v2/csat` (F5 §2.7)."""
        ...
