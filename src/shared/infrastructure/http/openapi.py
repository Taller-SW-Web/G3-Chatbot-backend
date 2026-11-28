"""OpenAPI contract that mirrors the real runtime (backend#253).

FastAPI cannot express three runtime facts on its own, so this module documents them
without changing any runtime behavior:

* bearer authentication: required (`require_customer`, `get_current_customer`) or optional
  (`get_caller_identity`: anonymous visitors identified by the `chat_sid` cookie are allowed);
* `Idempotency-Key` headers that the handlers validate by hand (a missing header is a problem
  error, so the schema must say `required: true`);
* the error body: every error is `application/problem+json` (`Problem` schema), and request
  validation answers `400 VALIDACION`, never FastAPI's default `422 HTTPValidationError`.

Security is attached per operation by inspecting the real dependency tree of each route; it is
never applied globally because anonymous routes (chat, catalog) exist.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, routing
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from src.shared.infrastructure.http.dependencies.auth_guard import require_customer
from src.shared.infrastructure.http.dependencies.caller_identity import get_caller_identity
from src.shared.infrastructure.http.dependencies.jwt_validator import get_current_customer

BEARER_SCHEME = "bearerAuth"
PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_REF = "#/components/schemas/Problem"
IDEMPOTENCY_HEADER = "Idempotency-Key"
_IDEMPOTENCY_MARKER = "x-idempotency-key-required"

_REQUIRED_AUTH_DEPENDENCIES = frozenset({require_customer, get_current_customer})
_OPTIONAL_AUTH_DEPENDENCIES = frozenset({get_caller_identity})

_STATUS_DESCRIPTIONS = {
    400: "Invalid request (problem+json, code VALIDACION or a module-specific code)",
    401: "Missing, malformed or invalid bearer token, or a session is required (problem+json)",
    402: "Payment rejected (problem+json)",
    403: "Account or action not allowed (problem+json)",
    404: "Resource not found or not available to the caller (problem+json)",
    409: "Conflict with the current state of the resource (problem+json)",
    410: "Resource or code expired (problem+json)",
    422: "Request understood but cannot be processed (problem+json)",
    429: "Too many requests; honor the Retry-After header (problem+json)",
    501: "Functionality not available (problem+json)",
    503: "Module or service temporarily unavailable (problem+json)",
}

PROBLEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Problem",
    "description": (
        "Error body served as application/problem+json. Modules may add fields beyond the "
        "ones listed (for example `reintentarEn`, `retryAfter`)."
    ),
    "required": ["status", "code", "title"],
    "properties": {
        "status": {"type": "integer", "description": "HTTP status code of the response."},
        "code": {"type": "string", "description": "Stable machine-readable error code."},
        "title": {"type": "string", "description": "Short summary of the problem type."},
        "detail": {
            "type": ["string", "null"],
            "description": "Human-readable explanation, when there is one.",
        },
        "errores": {
            "type": "array",
            "description": "Per-field validation errors (code VALIDACION / DATO_INVALIDO).",
            "items": {
                "type": "object",
                "required": ["campo", "mensaje"],
                "properties": {"campo": {"type": "string"}, "mensaje": {"type": "string"}},
            },
        },
        "campo": {"type": "string", "description": "Offending field or header name."},
        "modulo": {"type": "string", "description": "Module that is unavailable (503)."},
        "correlationId": {
            "type": "string",
            "description": (
                "Request correlation id (same value as the X-Correlation-Id header), "
                "present on 5xx responses for support."
            ),
        },
    },
    "additionalProperties": True,
}


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """`responses=` entries for operations that can answer these statuses as a problem."""
    return {
        status: {
            "description": _STATUS_DESCRIPTIONS.get(status, "Error (problem+json)"),
            "content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": PROBLEM_REF}}},
        }
        for status in statuses
    }


def idempotency_key_required() -> dict[str, Any]:
    """`openapi_extra=` marker for routes whose handler rejects a missing Idempotency-Key."""
    return {_IDEMPOTENCY_MARKER: True}


def _dependency_calls(route: Any) -> set[Any]:
    calls: set[Any] = set()
    pending = [route.dependant]
    while pending:
        current = pending.pop()
        for dependency in current.dependencies:
            if dependency.call is not None:
                calls.add(dependency.call)
            pending.append(dependency)
    return calls


def _security_requirement(route: Any) -> list[dict[str, list[str]]] | None:
    calls = _dependency_calls(route)
    if calls & _REQUIRED_AUTH_DEPENDENCIES:
        return [{BEARER_SCHEME: []}]
    if calls & _OPTIONAL_AUTH_DEPENDENCIES:
        # `{}` means "no credentials" is an accepted alternative (anonymous visitor).
        return [{}, {BEARER_SCHEME: []}]
    return None


def _set_idempotency_parameter(operation: dict[str, Any]) -> None:
    parameters = operation.setdefault("parameters", [])
    description = (
        "Client-generated unique key (for example a UUID). Retrying with the same key never "
        "repeats the operation. The server answers 400 when the header is missing."
    )
    for parameter in parameters:
        if parameter.get("in") == "header" and parameter.get("name") == IDEMPOTENCY_HEADER:
            parameter["required"] = True
            parameter["description"] = description
            parameter["schema"] = {"type": "string", "minLength": 1}
            parameter.pop("deprecated", None)
            return
    parameters.append(
        {
            "name": IDEMPOTENCY_HEADER,
            "in": "header",
            "required": True,
            "description": description,
            "schema": {"type": "string", "minLength": 1},
        }
    )


def _is_default_validation_error(response: dict[str, Any]) -> bool:
    media = response.get("content", {}).get("application/json", {})
    return media.get("schema", {}).get("$ref", "").endswith("/HTTPValidationError")


def _add_missing(responses: dict[str, Any], *statuses: int) -> None:
    for status, response in problem_responses(*statuses).items():
        responses.setdefault(str(status), response)


def _decorate_operation(operation: dict[str, Any], route: Any) -> None:
    responses = operation.setdefault("responses", {})
    if operation.pop(_IDEMPOTENCY_MARKER, None):
        _set_idempotency_parameter(operation)

    security = _security_requirement(route)
    if security is not None:
        operation["security"] = security
        _add_missing(responses, 401, 403)

    if "422" in responses and _is_default_validation_error(responses["422"]):
        del responses["422"]
    if "parameters" in operation or "requestBody" in operation:
        _add_missing(responses, 400)


def _prune_validation_schemas(schema: dict[str, Any]) -> None:
    schemas = schema.get("components", {}).get("schemas", {})
    if "HTTPValidationError" not in schemas:
        return
    if "HTTPValidationError" in str(schema.get("paths", {})):
        return
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)


def build_contract_schema(app: FastAPI) -> dict[str, Any]:
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
    )
    # FastAPI >= 0.165 keeps routers lazily mounted: resolve each route with its mount context.
    for route in routing.iter_route_contexts(app.routes):
        if not isinstance(route.original_route, APIRoute) or not route.include_in_schema:
            continue
        path_item = schema["paths"].get(route.path_format, {})
        for method in route.methods or ():
            operation = path_item.get(method.lower())
            if operation is not None:
                _decorate_operation(operation, route)
    components = schema.setdefault("components", {})
    components.setdefault("schemas", {})["Problem"] = PROBLEM_SCHEMA
    components["securitySchemes"] = {
        BEARER_SCHEME: {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Customer access token: `Authorization: Bearer <JWT>`.",
        }
    }
    _prune_validation_schemas(schema)
    return schema


def install_openapi_contract(app: FastAPI) -> None:
    """Replace `app.openapi` so `/openapi.json` and the exported file share this contract."""

    def _openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_contract_schema(app)
        return app.openapi_schema

    app.openapi = _openapi  # type: ignore[method-assign]
