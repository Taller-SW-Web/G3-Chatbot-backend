"""Chat tool for the local phone verification (SPEC-04).

The tool only reports the current state or opens the OTP form; the code itself
never travels through the LLM. The form posts directly to
`/contacto/celular/verificar-otp`.
"""
from typing import Any

from src.shared.application.tools.tool_context import caller_from_context
from src.shared.application.tools.tool_registry import ExecutionContext, ToolResult


def build_phone_handlers(phone_service) -> dict[str, Any]:
    async def verificar_celular(args, context: ExecutionContext) -> ToolResult:
        caller = caller_from_context(context)
        if caller is None or caller.customer_id is None or not context.access_token:
            return ToolResult(
                "REQUIERE_SESION",
                {
                    "texto": "Inicia sesión para verificar tu celular.",
                    "accionPendiente": {"herramienta": "verificar_celular", "argumentos": {}},
                },
            )
        try:
            status = await phone_service.status(context.access_token, caller.customer_id)
        except Exception:
            return ToolResult(
                "OK",
                {"texto": "No pudimos confirmar tu celular ahora. Intenta de nuevo en unos segundos."},
            )
        if status.get("celularVerificado"):
            return ToolResult("OK", {"texto": "Tu celular ya está verificado."})
        masked = str(status.get("celularEnmascarado") or "tu celular")
        return ToolResult(
            "OK",
            {
                "texto": f"Para coordinar la entrega necesitamos confirmar tu celular {masked}.",
                "bloques": [
                    {
                        "tipo": "FORMULARIO",
                        "formulario": "OTP_CELULAR",
                        "celularEnmascarado": masked,
                    }
                ],
            },
        )

    return {"verificar_celular": verificar_celular}
