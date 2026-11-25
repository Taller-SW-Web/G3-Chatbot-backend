"""Public surface of `mod-postventa`.

`mod-chat` may build post-sale tools from here; no other module imports the
service today, so the surface is intentionally small.
"""
from src.modules.mod_postventa.application.postventa_dto import (
    CLAIM_REASONS,
    ClaimDetailResponse,
    ClaimReason,
    PostventaError,
    ReturnDetailResponse,
)
from src.modules.mod_postventa.application.postventa_service import PostventaService
from src.modules.mod_postventa.domain.status_labels import (
    CLAIM_STATUS_LABELS,
    OPEN_CLAIM_STATES,
    RETURN_STATUS_LABELS,
    map_claim_status_label,
    map_return_status_label,
)
from src.modules.mod_postventa.module import provide_postventa_service
from src.modules.mod_postventa.ports.postventa_port import PostventaPort

__all__ = [
    "CLAIM_REASONS",
    "CLAIM_STATUS_LABELS",
    "ClaimDetailResponse",
    "ClaimReason",
    "OPEN_CLAIM_STATES",
    "PostventaError",
    "PostventaPort",
    "PostventaService",
    "RETURN_STATUS_LABELS",
    "ReturnDetailResponse",
    "map_claim_status_label",
    "map_return_status_label",
    "provide_postventa_service",
]
