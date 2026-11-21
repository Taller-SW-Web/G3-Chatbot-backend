"""Liveness/readiness probe. Deliberately does not touch the database."""
from fastapi import APIRouter

from src.shared.infrastructure.di.registry import registry

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    missing = registry.optional_missing
    if missing:
        return {"status": "degraded", "missingCapabilities": list(missing)}
    return {"status": "ok"}
