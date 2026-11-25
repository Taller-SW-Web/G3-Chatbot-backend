"""Tool catalog contract (SPEC-05 Req. 3 and 6).

The 29 names below are the only valid tool names exposed to the LLM; there are
no aliases. `requires_session` is enforced by `ToolRegistry` before any handler
runs. `requires_confirmation` mirrors the spec's "Confirmación en la UI" column:
`vaciar_carrito` is the only one the registry gates by itself; `iniciar_checkout`
and `preparar_*` are confirmed by their dedicated UI surfaces (`CheckoutPage`
and the "Enviar" button that dispatches `CONFIRMAR_*`).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolContract:
    name: str
    use_case: str
    requires_session: bool = False
    requires_confirmation: bool = False


TOOL_CONTRACTS: tuple[ToolContract, ...] = (
    ToolContract("buscar_productos", "manage_catalog"),
    ToolContract("recomendar_productos", "manage_catalog"),
    ToolContract("consultar_promociones", "manage_catalog"),
    ToolContract("ver_detalle_producto", "manage_catalog"),
    ToolContract("consultar_disponibilidad", "manage_catalog"),
    ToolContract("silenciar_complementos", "manage_catalog"),
    ToolContract("agregar_al_carrito", "manage_cart"),
    ToolContract("ver_carrito", "manage_cart"),
    ToolContract("cambiar_cantidad", "manage_cart"),
    ToolContract("quitar_del_carrito", "manage_cart"),
    ToolContract("vaciar_carrito", "manage_cart", requires_confirmation=True),
    ToolContract("iniciar_checkout", "manage_checkout", requires_session=True, requires_confirmation=True),
    ToolContract("aplicar_cupon", "manage_cart", requires_session=True),
    ToolContract("quitar_cupon", "manage_cart", requires_session=True),
    ToolContract("consultar_pedidos", "manage_orders", requires_session=True),
    ToolContract("consultar_seguimiento", "manage_orders", requires_session=True),
    ToolContract("preparar_reclamo", "manage_postventa", requires_session=True, requires_confirmation=True),
    ToolContract("listar_reclamos", "manage_postventa", requires_session=True),
    ToolContract("consultar_reclamo", "manage_postventa", requires_session=True),
    ToolContract("preparar_devolucion", "manage_postventa", requires_session=True, requires_confirmation=True),
    ToolContract("listar_devoluciones", "manage_postventa", requires_session=True),
    ToolContract("consultar_devolucion", "manage_postventa", requires_session=True),
    ToolContract("solicitar_registro", "manage_session"),
    ToolContract("solicitar_login", "manage_session"),
    ToolContract("reenviar_verificacion", "manage_session"),
    ToolContract("verificar_celular", "manage_phone_verification", requires_session=True),
    ToolContract("cerrar_sesion", "manage_session", requires_session=True),
    ToolContract("nueva_conversacion", "manage_conversation"),
    ToolContract("buscar_conversaciones", "manage_conversation"),
)


def get_tool_contract(name: str) -> ToolContract:
    for contract in TOOL_CONTRACTS:
        if contract.name == name:
            return contract
    raise KeyError(f"unknown tool {name!r} (tool catalog)")
