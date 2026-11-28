"""`mod-postventa` wiring: returns, claims, evidence upload and CSAT (SPEC-19..22)."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_orders.public import provide_sales_port
from src.modules.mod_postventa import MODULE_ID
from src.modules.mod_postventa.adapters.claim_ref_postgres_adapter import ClaimRefPostgresAdapter
from src.modules.mod_postventa.adapters.evidence_postgres_adapter import EvidencePostgresAdapter
from src.modules.mod_postventa.adapters.fake_postventa import FakePostventaPort
from src.modules.mod_postventa.adapters.http.claims_router import create_claims_router
from src.modules.mod_postventa.adapters.http.csat_router import create_csat_router
from src.modules.mod_postventa.adapters.http.returns_router import create_returns_router
from src.modules.mod_postventa.adapters.postventa_client import PostventaClient
from src.modules.mod_postventa.adapters.return_ref_postgres_adapter import ReturnRefPostgresAdapter
from src.modules.mod_postventa.application.draft_purge_service import DraftPurgeService
from src.modules.mod_postventa.application.postventa_service import PostventaService
from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.di.capabilities import POSTVENTA_DRAFT_PURGE
from src.shared.infrastructure.http.router_options import RouterOptions
from src.shared.infrastructure.persistence.conversation_postgres_adapter import ConversationPostgresAdapter
from src.shared.infrastructure.persistence.order_ref_postgres_adapter import OrderRefPostgresAdapter
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

_FAKE_POSTVENTA_PORT = FakePostventaPort()


def provide_postventa_port(settings: Settings | None = None):
    """G5 M2 Postventa: in-process fake in `SALES_MODE=fake`, HTTP otherwise."""
    settings = settings or get_settings()
    if settings.module_mode("sales") == "fake":
        return _FAKE_POSTVENTA_PORT
    return PostventaClient(
        base_url=settings.postventa_base_url,
        timeout_seconds=settings.postventa_timeout_seconds,
    )


def provide_postventa_service(session: AsyncSession = Depends(get_session)):
    """Postventa service with local idempotency and customer-token strategy."""
    settings = get_settings()
    fake = settings.module_mode("sales") == "fake"
    return PostventaService(
        postventa=provide_postventa_port(settings),
        sales=provide_sales_port(settings),
        claim_refs=None if fake else ClaimRefPostgresAdapter(session),
        return_refs=None if fake else ReturnRefPostgresAdapter(session),
        evidence=None if fake else EvidencePostgresAdapter(session),
        order_refs=None if fake else OrderRefPostgresAdapter(session),
        conversations=None if fake else ConversationPostgresAdapter(session),
        uow=SqlAlchemyUnitOfWork(session),
    )


def provide_draft_purge(session: AsyncSession) -> DraftPurgeService:
    """24 h purge of local drafts, driven by `outbox_worker_lifespan` (#265)."""
    return DraftPurgeService(
        evidence=EvidencePostgresAdapter(session),
        uow=SqlAlchemyUnitOfWork(session),
    )


def build_router(options: RouterOptions) -> APIRouter:
    """One module, three routers: `/evidencias|/devoluciones`, `/reclamos`, `/pedidos/{id}/csat`."""
    router = APIRouter()
    router.include_router(create_returns_router(provide_postventa_service))
    router.include_router(create_claims_router(provide_postventa_service))
    router.include_router(create_csat_router(provide_postventa_service))
    return router


router = build_router(RouterOptions())


def configure(container) -> None:
    """Postventa exposes the periodic draft purge; cross-module reads go through orders."""
    container.register(POSTVENTA_DRAFT_PURGE, provide_draft_purge)


__all__ = [
    "MODULE_ID",
    "POSTVENTA_DRAFT_PURGE",
    "build_router",
    "configure",
    "provide_draft_purge",
    "provide_postventa_port",
    "provide_postventa_service",
    "router",
]
