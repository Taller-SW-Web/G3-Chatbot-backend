"""Hito 3 tool catalog (H3-05 contract, implemented by H3-11).

Each entry binds a tool name to the owning use case plus the SPEC-05 Req. 6
flags the ToolRegistry enforces: `requires_session` and
`requires_confirmation`. Direct button actions reuse the same entries
through the ActionDispatcher, without the LLM.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolContract:
    name: str
    use_case: str
    requires_session: bool = False
    requires_confirmation: bool = False


HITO3_TOOL_CONTRACTS: tuple[ToolContract, ...] = (
    ToolContract("buscar_productos", "manage_catalog"),
    ToolContract("ver_detalle_producto", "manage_catalog"),
    ToolContract("consultar_disponibilidad", "manage_catalog"),
    ToolContract("consultar_promociones", "manage_catalog"),
    ToolContract("agregar_al_carrito", "manage_cart"),
    ToolContract("ver_carrito", "manage_cart"),
    ToolContract("cambiar_cantidad", "manage_cart"),
    ToolContract("quitar_del_carrito", "manage_cart"),
    ToolContract("vaciar_carrito", "manage_cart", requires_confirmation=True),
    ToolContract("solicitar_registro", "manage_session"),
    ToolContract("solicitar_login", "manage_session"),
    ToolContract("reenviar_verificacion", "manage_session"),
    ToolContract("cerrar_sesion", "manage_session", requires_session=True),
    ToolContract("iniciar_checkout", "manage_checkout", requires_session=True, requires_confirmation=True),
    ToolContract("nueva_conversacion", "manage_conversation"),
    ToolContract("buscar_conversaciones", "manage_conversation"),
)


def get_tool_contract(name: str) -> ToolContract:
    for contract in HITO3_TOOL_CONTRACTS:
        if contract.name == name:
            return contract
    raise KeyError(f"unknown tool {name!r} (Hito 3 catalog)")
