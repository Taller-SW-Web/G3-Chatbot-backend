"""Unit tests for OpenAPI schema export and /docs publication (H4-A2)."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from scripts.export_openapi import (
    DEFAULT_OUTPUT_PATH,
    build_openapi_schema,
    export_openapi,
    main,
    render_openapi_json,
)
from src.infrastructure.main import create_app

EXPECTED_H4_PATHS = {
    "/health",
    "/api/v1/sesion/login",
    "/api/v1/sesion/mfa/solicitar",
    "/api/v1/sesion/mfa/verificar",
    "/api/v1/sesion/refresh",
    "/api/v1/sesion/logout",
    "/api/v1/sesion/perfil",
    "/api/v1/sesion/registro",
    "/api/v1/sesion/politica-contrasena",
    "/api/v1/sesion/verificar-correo",
    "/api/v1/sesion/verificar-correo/reenviar",
    "/api/v1/chat/conversaciones",
    "/api/v1/chat/conversaciones/buscar",
    "/api/v1/chat/conversaciones/{conversation_id}/mensajes",
    "/api/v1/catalogo/productos",
    "/api/v1/catalogo/productos/{product_id}",
    "/api/v1/catalogo/disponibilidad",
    "/api/v1/carrito",
    "/api/v1/carrito/items",
    "/api/v1/carrito/items/{item_id}",
    "/api/v1/carrito/cupon",
    "/api/v1/pedidos",
    "/api/v1/pedidos/{pedido_id}",
    "/api/v1/pedidos/{pedido_id}/seguimiento",
    "/api/v1/pedidos/{pedido_id}/reenviar-confirmacion",
    "/api/v1/checkout",
    "/api/v1/checkout/{checkout_id}",
    "/api/v1/checkout/{checkout_id}/pago",
    "/api/v1/direcciones",
    "/api/v1/envio/cotizar",
}


def test_build_openapi_schema_includes_all_contract_paths() -> None:
    schema = build_openapi_schema()

    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"] == "G3 Chatbot backend"
    paths = set(schema["paths"].keys())
    assert EXPECTED_H4_PATHS.issubset(paths)


def test_export_openapi_writes_deterministic_json_and_check_mode(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"

    written = export_openapi(target)
    assert written == target.resolve()
    assert target.is_file()

    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert EXPECTED_H4_PATHS.issubset(parsed["paths"].keys())
    assert target.read_text(encoding="utf-8") == render_openapi_json(parsed)

    assert main(["--output", str(target), "--check"]) == 0


def test_committed_docs_openapi_json_exists_and_is_valid() -> None:
    assert DEFAULT_OUTPUT_PATH.is_file()
    parsed = json.loads(DEFAULT_OUTPUT_PATH.read_text(encoding="utf-8"))
    assert EXPECTED_H4_PATHS.issubset(parsed["paths"].keys())


def test_fastapi_serves_swagger_docs_and_openapi_json() -> None:
    client = TestClient(create_app())

    docs_res = client.get("/docs")
    assert docs_res.status_code == 200
    assert "Swagger UI" in docs_res.text

    openapi_res = client.get("/openapi.json")
    assert openapi_res.status_code == 200
    assert EXPECTED_H4_PATHS.issubset(openapi_res.json()["paths"].keys())
