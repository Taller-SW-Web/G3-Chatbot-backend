"""Commercial catalog handlers for the tool registry (SPEC-06, SPEC-09, SPEC-10).

Each handler validates through its Pydantic schema upstream (the registry),
delegates to `ManageCatalogUseCase` and answers `ToolResult(code, data)`
with user-facing `texto` plus structured `bloques`. Identity always comes
from the execution context, never from LLM arguments.
"""
import uuid

from src.application.tools.tool_context import (
    caller_from_context,
    conversation_id_from_context,
)
from src.application.tools.tool_registry import ExecutionContext, ToolResult
from src.application.use_cases.manage_catalog_use_case import CatalogError
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.outbound.module_errors import ModuleUnavailableError


def _caller(context: ExecutionContext) -> CallerIdentity | None:
    return caller_from_context(context)


def _conversation_id(context: ExecutionContext) -> uuid.UUID | None:
    return conversation_id_from_context(context)


def build_catalog_handlers(catalog_use_case) -> dict:
    async def buscar_productos(args, context: ExecutionContext) -> ToolResult:
        try:
            result = await catalog_use_case.search(args, context)
        except CatalogError as exc:
            return ToolResult(exc.code, {"texto": exc.detail})
        except ModuleUnavailableError as exc:
            return ToolResult("SERVICIO_NO_DISPONIBLE", {"modulo": exc.module})
        return ToolResult(result["code"], {
            "texto": result["texto"], "bloques": result["bloques"],
        })

    async def ver_detalle_producto(args, context: ExecutionContext) -> ToolResult:
        try:
            result = await catalog_use_case.detail(args, context)
        except CatalogError as exc:
            return ToolResult(exc.code, {"texto": exc.detail})
        except ModuleUnavailableError as exc:
            return ToolResult("SERVICIO_NO_DISPONIBLE", {"modulo": exc.module})
        return ToolResult(result["code"], {
            "texto": result["texto"], "bloques": result["bloques"],
        })

    async def consultar_disponibilidad(args, context: ExecutionContext) -> ToolResult:
        try:
            result = await catalog_use_case.availability(args, context)
        except CatalogError as exc:
            return ToolResult(exc.code, {"texto": exc.detail})
        except ModuleUnavailableError as exc:
            return ToolResult("SERVICIO_NO_DISPONIBLE", {"modulo": exc.module})
        return ToolResult(result["code"], {
            "texto": result["texto"], "bloques": result["bloques"],
        })

    return {
        "buscar_productos": buscar_productos,
        "ver_detalle_producto": ver_detalle_producto,
        "consultar_disponibilidad": consultar_disponibilidad,
    }
