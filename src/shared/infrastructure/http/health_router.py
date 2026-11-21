"""Liveness (`/health`, no database) and readiness (`/health/ready`, `SELECT 1`) probes."""
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.di.registry import registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Liveness: deliberately does not touch the database."""
    missing = registry.optional_missing
    if missing:
        return {"status": "degraded", "missingCapabilities": list(missing)}
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(session: AsyncSession = Depends(get_session)):
    """Readiness: 200 only when the database answers `SELECT 1`, 503 otherwise."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        # Class name only: driver messages can echo connection details.
        logger.error("Readiness check failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "down"})
    return {"status": "ok", "database": "up"}
