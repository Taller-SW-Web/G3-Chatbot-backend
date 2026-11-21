"""Public surface of `mod-postventa`.

`mod-chat` may build post-sale tools from here; no other module imports the
service today, so the surface is intentionally small.
"""
from src.modules.mod_postventa.application.postventa_dto import (
    ClaimDetailResponse,
    ReturnDetailResponse,
)
from src.modules.mod_postventa.application.postventa_service import PostventaService
from src.modules.mod_postventa.module import provide_postventa_service
from src.modules.mod_postventa.ports.postventa_port import PostventaPort

__all__ = [
    "ClaimDetailResponse",
    "PostventaPort",
    "PostventaService",
    "ReturnDetailResponse",
    "provide_postventa_service",
]
