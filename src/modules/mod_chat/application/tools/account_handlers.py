"""Account, checkout and navigation tools that only open UI surfaces (SPEC-01..05, 14).

None of these handlers performs the browser effect: `IR_A_CHECKOUT`,
`CERRAR_SESION`, `NUEVO_CHAT` and `BUSCAR_CHATS` travel as quick actions and
the frontend owns navigation, the BFF logout and conversation creation.
"""

from typing import Any

from src.modules.mod_chat.application.tools.tool_blocks import actions, form
from src.shared.application.tools.tool_registry import ExecutionContext, ToolResult


def _action(action_id: str, label: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": action_id, "etiqueta": label, "payload": payload or {}}


def _login_required(action: str, texto: str) -> ToolResult:
    return ToolResult(
        "REQUIERE_SESION",
        {
            "texto": texto,
            "accionPendiente": action,
            "bloques": [form("LOGIN", {"accionPendiente": action})],
        },
    )


def build_account_handlers() -> dict[str, Any]:
    async def solicitar_login(args, context: ExecutionContext) -> ToolResult:
        if context.cliente_id:
            return ToolResult("OK", {"texto": "Ya tienes una sesión iniciada con tu cuenta."})
        return ToolResult("OK", {
            "texto": "Inicia sesión para continuar.",
            "bloques": [form("LOGIN")],
        })

    async def reenviar_verificacion(args, context: ExecutionContext) -> ToolResult:
        return ToolResult("OK", {
            "texto": "Si tu cuenta está pendiente de verificación, te enviaremos un enlace nuevo.",
            "bloques": [form("REENVIO_VERIFICACION")],
        })

    async def iniciar_checkout(args, context: ExecutionContext) -> ToolResult:
        if not context.cliente_id:
            return _login_required(
                "INICIAR_CHECKOUT", "Inicia sesión para continuar con el pago.",
            )
        return ToolResult("OK", {
            "texto": "Revisa tu dirección y confirma el pago en la pantalla de checkout.",
            "bloques": [actions(_action("IR_A_CHECKOUT", "Ir al checkout"))],
        })

    async def cerrar_sesion(args, context: ExecutionContext) -> ToolResult:
        if not context.cliente_id:
            return _login_required("CERRAR_SESION", "No hay una sesión activa que cerrar.")
        return ToolResult("OK", {
            "texto": "Pulsa \"Cerrar sesión\" para salir de tu cuenta; tus conversaciones anteriores siguen disponibles.",
            "bloques": [actions(_action("CERRAR_SESION", "Cerrar sesión"))],
        })

    async def nueva_conversacion(args, context: ExecutionContext) -> ToolResult:
        return ToolResult("OK", {
            "texto": "Puedes empezar una conversación nueva cuando quieras.",
            "bloques": [actions(_action("NUEVO_CHAT", "Nuevo chat"))],
        })

    async def buscar_conversaciones(args, context: ExecutionContext) -> ToolResult:
        query = str(args.q).strip()
        return ToolResult("OK", {
            "texto": f"Buscaré en tus conversaciones: {query}.",
            "bloques": [actions(_action("BUSCAR_CHATS", "Buscar chats", {"q": query}))],
        })

    return {
        "solicitar_login": solicitar_login,
        "reenviar_verificacion": reenviar_verificacion,
        "iniciar_checkout": iniciar_checkout,
        "cerrar_sesion": cerrar_sesion,
        "nueva_conversacion": nueva_conversacion,
        "buscar_conversaciones": buscar_conversaciones,
    }
