"""Evidencia viva de la integración con G5 (M1 Ventas + M2 Postventa).

Llama directo a los servicios M1/M2 (el gateway público queda detrás de Cloudflare y no
se usa por defecto), siempre con el token de **cliente**:

- M1 (`G5_SALES_BASE`): pedidos `/api/v1/pedidos/me` y detalle.
- M2 (`G5_POSTVENTA_BASE`): CSAT, devoluciones y reclamos `/api/v2/...`.

El token no se imprime ni se escribe en el reporte. El payload del JWT se decodifica
**sin verificar la firma** solo para usar `sub` como `clienteId` del CSAT.

Uso (PowerShell):

    $env:G5_BEARER_TOKEN = "..."            # access token de cliente (rol CLIENTE)
    python scripts/verify_g5_integration.py --dry-run
    python scripts/verify_g5_integration.py --execute

Variables opcionales: `G5_SALES_BASE`, `G5_POSTVENTA_BASE`. `--gateway-base` apunta
ambos servicios al gateway, como opción explícita (no es el default).

Los ids son parametrizables, pero sus defaults corresponden a la **cuenta 2**:
`--own-order`/`--csat-order`/`--return-order` = `PED-2026-90201`, `--foreign-order` =
`PED-2026-90101` (cuenta principal) y `--paid-order` = `PED-2026-90202` (CREADO, sirve
para el 422). El `G5_BEARER_TOKEN` debe ser de la cuenta 2; el script comprueba primero
qué pedidos lista `/pedidos/me` y omite (`SKIP`) los casos que no correspondan, en vez de
convertirlos en falsos fallos.

Salida: 0 si todos los casos ejecutados pasan, 1 si hay fallos, 2 si falta configuración.
`--dry-run` no hace llamadas de escritura ni escribe el reporte; sin token muestra el
plan con un marcador para poder revisarlo sin secretos.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

DEFAULT_SALES_BASE = "https://g5-servicio-ventas.onrender.com"
DEFAULT_POSTVENTA_BASE = "https://g5-ventas-postventas-backend.onrender.com"
DEFAULT_OWN_ORDER = "PED-2026-90201"
DEFAULT_FOREIGN_ORDER = "PED-2026-90101"
DEFAULT_CSAT_ORDER = "PED-2026-90201"
DEFAULT_RETURN_ORDER = "PED-2026-90201"
DEFAULT_PAID_ORDER = "PED-2026-90202"
DEFAULT_DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"

HEALTH_TIMEOUT_SECONDS = 180.0
HEALTH_POLL_SECONDS = 3.0
REQUEST_TIMEOUT_SECONDS = 60.0
EVIDENCE_SNIPPET_LIMIT = 280

OK = "OK"
FAIL = "FAIL"
SKIP = "SKIP"

CSAT_COMMENT = "Prueba automatizada G3: pedido entregado en buen estado."
RETURN_DESCRIPTION = "Prueba automatizada G3: la talla recibida no corresponde a la solicitada."
CLAIM_DETAIL = "Prueba automatizada G3: el pedido llegó después del plazo de entrega comprometido."

_SENSITIVE_KEYS = {
    "authorization",
    "access_token",
    "accessToken",
    "refresh_token",
    "refreshToken",
    "token",
    "bearer",
    "password",
    "clienteId",
    "sub",
    "documento",
    "email",
    "nombreCompleto",
    "telefono",
}

# 1x1 transparent PNG; M2 exige una evidencia real para las devoluciones.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ---------------------------------------------------------------------------
# Helpers (network-free)
# ---------------------------------------------------------------------------


def normalize_base_url(value: str | None, default: str) -> str:
    cleaned = (value or "").strip().rstrip("/")
    return cleaned or default


def build_url(base_url: str, path: str, params: dict[str, Any] | None = None) -> str:
    base = normalize_base_url(base_url, base_url)
    clean_path = "/" + path.lstrip("/")
    if not params:
        return f"{base}{clean_path}"
    query = urlencode({key: value for key, value in params.items() if value is not None})
    return f"{base}{clean_path}?{query}" if query else f"{base}{clean_path}"


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decodes the JWT payload WITHOUT verifying the signature; only `sub` is used."""
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or not parts[1]:
        raise ValueError("El token no tiene el formato de un JWT (tres segmentos).")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(
            "No pude decodificar el payload del JWT; revisa que G5_BEARER_TOKEN sea un access token."
        ) from exc
    if not isinstance(claims, dict):
        raise ValueError("El payload del JWT no es un objeto JSON.")
    return claims


def extract_subject(token: str) -> str:
    claims = decode_jwt_payload(token)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("El JWT no trae el claim `sub`; no puedo usarlo como clienteId.")
    return subject.strip()


def redact(value: Any, key: str | None = None) -> Any:
    if key is not None and key in _SENSITIVE_KEYS:
        return "***"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def snippet(payload: Any, limit: int = EVIDENCE_SNIPPET_LIMIT) -> str:
    if payload is None:
        text = ""
    elif isinstance(payload, (dict, list)):
        text = json.dumps(redact(payload), ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(payload)
    text = " ".join(text.split()).replace("|", "\\|").replace("`", "'")
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def evidence_path(now: datetime, docs_dir: str | Path = DEFAULT_DOCS_DIR) -> Path:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return Path(docs_dir) / f"evidencia-g5-{stamp}.md"


def build_csat_payload(order_id: str, cliente_id: str) -> dict[str, Any]:
    return {
        "pedidoId": order_id,
        "clienteId": cliente_id,
        "canal": "CHATBOT",
        "puntuacion": 5,
        "comentario": CSAT_COMMENT,
    }


def build_return_items(order_detail: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in order_detail.get("items") or []:
        if not isinstance(raw, dict):
            continue
        product_id = raw.get("productoId") or raw.get("productId")
        if not product_id:
            continue
        try:
            quantity = int(raw.get("cantidad") or 1)
        except (TypeError, ValueError):
            quantity = 1
        items.append({"productoId": str(product_id), "cantidad": max(quantity, 1)})
    return items


def build_return_payload(
    order_id: str, items: list[dict[str, Any]], evidence_url: str
) -> dict[str, Any]:
    return {
        "pedidoId": order_id,
        "tipo": "DEVOLUCION_DINERO",
        "motivo": "OTRO",
        "descripcion": RETURN_DESCRIPTION,
        "items": items,
        "evidencias": [{"tipo": "IMAGEN", "url": evidence_url}],
    }


def build_claim_payload(order_id: str) -> dict[str, Any]:
    return {
        "pedidoId": order_id,
        "tipo": "RECLAMO",
        "canal": "CHATBOT",
        "motivo": "INCUMPLIMIENTO_PLAZO_ENTREGA",
        "detalle": CLAIM_DETAIL,
        "consumidor": {
            "nombreCompleto": "Cliente Prueba G3",
            "documento": "72458912",
            "email": "cliente.prueba@example.com",
            "telefono": "+51999888777",
        },
    }


# ---------------------------------------------------------------------------
# Evidence rows
# ---------------------------------------------------------------------------


@dataclass
class CaseEvidence:
    case: str
    method: str
    path: str
    expected: str
    performed_at: datetime
    status_code: int | None = None
    resource_id: str | None = None
    result: str = FAIL
    detail: str = ""
    body_snippet: str = ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_body(response: httpx.Response | None) -> Any:
    if response is None:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text[:1000] if response.content else None


def _error_code(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("code") or body.get("codigo") or "")
    return ""


def _resource_id(body: Any, *keys: str) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in keys:
        value = body.get(key)
        if value:
            return str(value)
    return None


def _page_items(body: Any) -> list[Any] | None:
    if not isinstance(body, dict):
        return None
    for key in ("items", "contenido"):
        value = body.get(key)
        if isinstance(value, list):
            return value
    return None


def _page_total(body: Any) -> int | None:
    if not isinstance(body, dict):
        return None
    for key in ("total", "totalElementos"):
        value = body.get(key)
        if isinstance(value, int):
            return value
    return None


def _record(
    case: str,
    method: str,
    path: str,
    expected: str,
    response: httpx.Response | None,
    *,
    ok: bool,
    resource_id: str | None = None,
    detail: str = "",
) -> CaseEvidence:
    return CaseEvidence(
        case=case,
        method=method,
        path=path,
        expected=expected,
        performed_at=_utcnow(),
        status_code=response.status_code if response is not None else None,
        resource_id=resource_id,
        result=OK if ok else FAIL,
        detail=detail,
        body_snippet=snippet(_parse_body(response)),
    )


def _skipped(case: str, method: str, path: str, expected: str, detail: str) -> CaseEvidence:
    return CaseEvidence(
        case=case,
        method=method,
        path=path,
        expected=expected,
        performed_at=_utcnow(),
        result=SKIP,
        detail=detail,
    )


def probe_health(client: httpx.Client, base_url: str) -> tuple[bool, str]:
    try:
        response = client.get(build_url(base_url, "/actuator/health"))
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    body = _parse_body(response)
    if response.status_code == 200 and isinstance(body, dict) and str(body.get("status", "")).upper() == "UP":
        return True, "HTTP 200 status=UP"
    if isinstance(body, dict):
        return False, f"HTTP {response.status_code} status={body.get('status')}"
    return False, f"HTTP {response.status_code}"


def wait_for_health(
    client: httpx.Client,
    base_url: str,
    *,
    timeout_seconds: float = HEALTH_TIMEOUT_SECONDS,
    poll_seconds: float = HEALTH_POLL_SECONDS,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> tuple[bool, str]:
    deadline = monotonic() + timeout_seconds
    last_detail = "sin respuesta"
    while True:
        healthy, last_detail = probe_health(client, base_url)
        if healthy:
            return True, last_detail
        if monotonic() >= deadline:
            return False, last_detail
        sleep(poll_seconds)


def _plan_text(
    *,
    sales_base: str,
    postventa_base: str,
    subject: str | None,
    own_order: str,
    foreign_order: str,
    csat_order: str,
    return_order: str,
    paid_order: str,
) -> str:
    cliente = subject or "<sub del JWT: define G5_BEARER_TOKEN>"
    lines = [
        "Dry-run: no se hará ninguna llamada de escritura ni se escribirá el reporte.",
        f"M1 (pedidos): {sales_base}",
        f"M2 (postventa): {postventa_base}",
        f"Token de cliente: {'presente; sub=' + subject if subject else 'ausente (plan con marcador)'}",
        "Cuenta por defecto: 2 (el token debe ser de la cuenta 2; 90101 es la cuenta ajena)",
        f"Pedidos: propio={own_order}, ajeno={foreign_order}, CSAT={csat_order}, devolución={return_order}, PAGADO/CREADO={paid_order}",
        "",
        "1. GET  /actuator/health en M1 y M2 (hasta 180 s cada uno)",
        f"2. GET  {sales_base}/api/v1/pedidos/me?page=1&pageSize=50",
        f"3. GET  {sales_base}/api/v1/pedidos/me?estado=ENTREGADO&desde=2026-01-01&hasta=2026-12-31&page=1&pageSize=50",
        f"4. GET  {sales_base}/api/v1/pedidos/me?desde=2030-01-01&hasta=2030-12-31&page=1&pageSize=50 (rango vacío)",
        f"5. GET  {sales_base}/api/v1/pedidos/{own_order} (propio)",
        f"6. GET  {sales_base}/api/v1/pedidos/{foreign_order} (ajeno; espera 403)",
        f"7. POST {postventa_base}/api/v2/csat sobre {paid_order} (no entregado; espera 422 ORDER_NOT_DELIVERED)",
        f"8. POST {postventa_base}/api/v2/csat sobre {csat_order} (espera 201 o 409 si ya estaba calificado)",
        f"9. POST {postventa_base}/api/v2/csat sobre {csat_order} otra vez (espera 409 CSAT_ALREADY_SUBMITTED)",
        f"10. POST {postventa_base}/api/v2/devoluciones/evidencias/upload (PNG real)",
        f"11. GET {sales_base}/api/v1/pedidos/{return_order} (items para la devolución)",
        f"12. POST {postventa_base}/api/v2/devoluciones con motivo OTRO y evidencias[0] (espera 201)",
        f"13. GET {postventa_base}/api/v2/devoluciones/me?pagina=0&tamano=50 (espera 200)",
        f"14. GET {postventa_base}/api/v2/devoluciones/{{id}} (espera 200)",
        f"15. POST {postventa_base}/api/v2/reclamos sobre {own_order} (espera 201; guarda codigoSeguimiento)",
        f"16. GET {postventa_base}/api/v2/reclamos/me?pagina=0&tamano=50 (espera 200)",
        f"17. GET {postventa_base}/api/v2/reclamos/{{codigoSeguimiento}} (espera 200)",
        "",
        "Cuerpos de escritura:",
        "POST /api/v2/csat",
        json.dumps(build_csat_payload(csat_order, cliente), indent=2, ensure_ascii=False),
        "",
        "POST /api/v2/devoluciones",
        json.dumps(build_return_payload(return_order, [{"productoId": "<items[].productoId>", "cantidad": "<cantidad>"}], "<url de evidencias>"), indent=2, ensure_ascii=False),
        "",
        "POST /api/v2/reclamos",
        json.dumps(build_claim_payload(own_order), indent=2, ensure_ascii=False),
    ]
    return "\n".join(lines)


def render_markdown(
    rows: list[CaseEvidence],
    *,
    sales_base: str,
    postventa_base: str,
    generated_at: datetime,
) -> str:
    failures = [row for row in rows if row.result == FAIL]
    total = len(rows)
    passed = sum(1 for row in rows if row.result == OK)
    skipped = sum(1 for row in rows if row.result == SKIP)
    lines = [
        "# Evidencia de integración con G5 (M1 + M2, token de cliente)",
        "",
        f"- Fecha UTC: {format_utc(generated_at)}",
        f"- M1 (pedidos): `{sales_base}`",
        f"- M2 (postventa): `{postventa_base}`",
        "- Token de cliente: presente (no se registra en este documento)",
        f"- Resultado: **{passed}/{total} OK**, {len(failures)} fallo(s), {skipped} omitido(s)",
        "",
        "| Hora UTC | Caso | Método | Ruta | Esperado | HTTP | Recurso | Resultado | Snippet |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        detail = f"{row.detail} " if row.detail else ""
        body = f"`{detail}{row.body_snippet}`" if (detail or row.body_snippet) else ""
        lines.append(
            "| {time} | {case} | {method} | `{path}` | {expected} | {status} | {resource} | {result} | {body} |".format(
                time=format_utc(row.performed_at),
                case=row.case.replace("|", "\\|"),
                method=row.method,
                path=row.path,
                expected=row.expected,
                status=row.status_code if row.status_code is not None else "—",
                resource=(row.resource_id or "").replace("|", "\\|"),
                result=row.result,
                body=body,
            )
        )
    lines.extend(["", "## Fallos", ""])
    if failures:
        for row in failures:
            lines.append(
                f"- **{row.case}** — esperaba {row.expected}, obtuvo "
                f"{row.status_code if row.status_code is not None else 'sin respuesta'}. "
                f"{row.detail or 'sin detalle'}"
            )
    else:
        lines.append("Ninguno.")
    lines.extend(
        [
            "",
            "## Notas",
            "",
            "- Los casos de M1 usan el token de cliente contra `GET /api/v1/pedidos/me` y el detalle.",
            "- Los casos de M2 usan el mismo token contra CSAT, devoluciones y reclamos.",
            "- La firma del JWT no se verifica: solo se lee `sub` para `clienteId` del CSAT.",
            "- Los snippets están truncados y redactan token, `clienteId`, `sub` y datos del consumidor.",
            "- La devolución usa `motivo: OTRO` directo contra G5 y evidencia real subida antes.",
            "- `desde`/`hasta` se envían a M1; se comprueban con un rango con datos y con un",
            "  rango futuro que debe volver vacío, además del filtro `estado`.",
            "- Un `SKIP` indica un pedido que no aparece en `/pedidos/me` para ese token o una",
            "  precondición que no se pudo cumplir; el reporte indica el motivo.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Live flow
# ---------------------------------------------------------------------------


def _run_live(
    *,
    client: httpx.Client,
    sales_base: str,
    postventa_base: str,
    subject: str,
    own_order: str,
    foreign_order: str,
    csat_order: str,
    return_order: str,
    paid_order: str,
) -> list[CaseEvidence]:
    rows: list[CaseEvidence] = []

    sales_healthy, sales_detail = wait_for_health(client, sales_base)
    rows.append(
        CaseEvidence(
            case="Salud M1",
            method="GET",
            path="/actuator/health",
            expected="200 status=UP (hasta 180 s)",
            performed_at=_utcnow(),
            status_code=200 if sales_healthy else None,
            result=OK if sales_healthy else FAIL,
            detail=sales_detail,
        )
    )
    postventa_healthy, postventa_detail = wait_for_health(client, postventa_base)
    rows.append(
        CaseEvidence(
            case="Salud M2",
            method="GET",
            path="/actuator/health",
            expected="200 status=UP (hasta 180 s)",
            performed_at=_utcnow(),
            status_code=200 if postventa_healthy else None,
            result=OK if postventa_healthy else FAIL,
            detail=postventa_detail,
        )
    )
    if not sales_healthy or not postventa_healthy:
        return rows

    # 1. Listado base + ids que el token realmente posee.
    path = "/api/v1/pedidos/me?page=1&pageSize=50"
    response = client.get(build_url(sales_base, "/api/v1/pedidos/me", {"page": 1, "pageSize": 50}))
    body = _parse_body(response)
    items = _page_items(body)
    total = _page_total(body)
    shape_ok = (
        response.status_code == 200
        and isinstance(body, dict)
        and items is not None
        and total is not None
        and total >= len(items)
        and body.get("page") == 1
        and body.get("pageSize") == 50
    )
    owned_ids = {
        str(item.get("pedidoId"))
        for item in (items or [])
        if isinstance(item, dict) and item.get("pedidoId")
    }
    rows.append(
        _record(
            "M1 · Listado de pedidos",
            "GET",
            path,
            "200 {items,total,page,pageSize}",
            response,
            ok=shape_ok,
            resource_id=f"{total} pedido(s)" if total is not None else None,
            detail="" if shape_ok else "La respuesta no tiene la forma acordada.",
        )
    )

    # 2. Filtros estado/desde/hasta + paginación.
    filter_params = {
        "estado": "ENTREGADO",
        "desde": "2026-01-01",
        "hasta": "2026-12-31",
        "page": 1,
        "pageSize": 50,
    }
    filter_path = "/api/v1/pedidos/me?" + urlencode(filter_params)
    response = client.get(build_url(sales_base, "/api/v1/pedidos/me", filter_params))
    body = _parse_body(response)
    filtered = _page_items(body)
    filter_total = _page_total(body)
    filter_ok = (
        response.status_code == 200
        and filtered is not None
        and isinstance(body, dict)
        and body.get("page") == 1
        and body.get("pageSize") == 50
        and filter_total is not None
        and filter_total >= len(filtered)
    )
    states_ok = all(
        str(item.get("estado", "")).upper() == "ENTREGADO"
        for item in (filtered or [])
        if isinstance(item, dict)
    )
    rows.append(
        _record(
            "M1 · Filtros estado/desde/hasta",
            "GET",
            filter_path,
            "200 y solo ENTREGADO",
            response,
            ok=filter_ok and states_ok,
            resource_id=f"{len(filtered or [])} fila(s)",
            detail="" if filter_ok and states_ok else "El filtro no respetó estado o paginación.",
        )
    )

    # 3. Rango futuro: si `desde`/`hasta` se aplican, no hay pedidos.
    future_params = {"desde": "2030-01-01", "hasta": "2030-12-31", "page": 1, "pageSize": 50}
    future_path = "/api/v1/pedidos/me?" + urlencode(future_params)
    response = client.get(build_url(sales_base, "/api/v1/pedidos/me", future_params))
    body = _parse_body(response)
    future_items = _page_items(body)
    future_total = _page_total(body)
    future_ok = (
        response.status_code == 200
        and future_items == []
        and future_total == 0
    )
    rows.append(
        _record(
            "M1 · Rango de fechas vacío",
            "GET",
            future_path,
            "200 sin pedidos",
            response,
            ok=future_ok,
            resource_id="0 pedido(s)" if future_total == 0 else None,
            detail="" if future_ok else "`desde`/`hasta` no filtraron el rango futuro.",
        )
    )

    # 3. Detalle propio y ajeno.
    own_path = f"/api/v1/pedidos/{quote(own_order, safe='')}"
    response = client.get(build_url(sales_base, own_path))
    own_body = _parse_body(response)
    own_ok = (
        response.status_code == 200
        and isinstance(own_body, dict)
        and bool(own_body.get("historial") or own_body.get("historialEstados"))
    )
    rows.append(
        _record(
            "M1 · Detalle de pedido propio",
            "GET",
            own_path,
            "200 con historial",
            response,
            ok=own_ok,
            resource_id=own_order,
            detail="" if own_ok else "El detalle no trae historial.",
        )
    )

    if foreign_order in owned_ids:
        rows.append(
            _skipped(
                "M1 · Detalle de pedido ajeno",
                "GET",
                f"/api/v1/pedidos/{quote(foreign_order, safe='')}",
                "403",
                f"{foreign_order} pertenece a este token según /pedidos/me; pasa otro --foreign-order.",
            )
        )
    else:
        foreign_path = f"/api/v1/pedidos/{quote(foreign_order, safe='')}"
        response = client.get(build_url(sales_base, foreign_path))
        foreign_body = _parse_body(response)
        rows.append(
            _record(
                "M1 · Detalle de pedido ajeno",
                "GET",
                foreign_path,
                "403",
                response,
                ok=response.status_code == 403,
                resource_id=foreign_order,
                detail="" if response.status_code == 403 else f"code={_error_code(foreign_body)}",
            )
        )

    # 4. CSAT 422 con un pedido no entregado (CREADO/PAGADO; no crea nada).
    if paid_order not in owned_ids:
        rows.append(
            _skipped(
                "M2 · CSAT de pedido no entregado",
                "POST",
                "/api/v2/csat",
                "422 ORDER_NOT_DELIVERED",
                f"{paid_order} no aparece en /pedidos/me para este token.",
            )
        )
    else:
        paid_payload = build_csat_payload(paid_order, subject)
        response = client.post(build_url(postventa_base, "/api/v2/csat"), json=paid_payload)
        body = _parse_body(response)
        paid_ok = response.status_code == 422 and _error_code(body) == "ORDER_NOT_DELIVERED"
        rows.append(
            _record(
                "M2 · CSAT de pedido no entregado",
                "POST",
                "/api/v2/csat",
                "422 ORDER_NOT_DELIVERED",
                response,
                ok=paid_ok,
                resource_id=paid_order,
                detail="" if paid_ok else f"code={_error_code(body)}",
            )
        )

    # 5. CSAT sobre el pedido entregado. Un rerun puede encontrar 409: es el estado
    # esperado si el 201 ya se evidenció antes.
    if csat_order not in owned_ids:
        rows.append(
            _skipped(
                "M2 · CSAT",
                "POST",
                "/api/v2/csat",
                "201 (o 409 si ya estaba calificado)",
                f"{csat_order} no aparece en /pedidos/me para este token.",
            )
        )
        csat_payload = build_csat_payload(csat_order, subject)
    else:
        csat_payload = build_csat_payload(csat_order, subject)
        response = client.post(build_url(postventa_base, "/api/v2/csat"), json=csat_payload)
        body = _parse_body(response)
        created = response.status_code == 201
        already = response.status_code == 409 and _error_code(body) == "CSAT_ALREADY_SUBMITTED"
        rows.append(
            _record(
                "M2 · CSAT",
                "POST",
                "/api/v2/csat",
                "201 (o 409 si ya estaba calificado)",
                response,
                ok=created or already,
                resource_id=csat_order,
                detail=(
                    ""
                    if created
                    else "Ya estaba calificado; el 201 corresponde a una corrida anterior."
                    if already
                    else f"code={_error_code(body)}"
                ),
            )
        )

    # 6. CSAT duplicado.
    if csat_order not in owned_ids:
        rows.append(
            _skipped(
                "M2 · CSAT duplicado",
                "POST",
                "/api/v2/csat",
                "409 CSAT_ALREADY_SUBMITTED",
                f"{csat_order} no aparece en /pedidos/me para este token.",
            )
        )
    else:
        response = client.post(build_url(postventa_base, "/api/v2/csat"), json=csat_payload)
        body = _parse_body(response)
        duplicate_ok = response.status_code == 409 and _error_code(body) == "CSAT_ALREADY_SUBMITTED"
        rows.append(
            _record(
                "M2 · CSAT duplicado",
                "POST",
                "/api/v2/csat",
                "409 CSAT_ALREADY_SUBMITTED",
                response,
                ok=duplicate_ok,
                resource_id=csat_order,
                detail="" if duplicate_ok else f"code={_error_code(body)}",
            )
        )

    # 7. Evidencia real (siempre) y devolución sobre el pedido entregado.
    upload_path = "/api/v2/devoluciones/evidencias/upload"
    upload = client.post(
        build_url(postventa_base, upload_path),
        files={"file": ("evidencia-g3.png", _PNG, "image/png")},
    )
    upload_body = _parse_body(upload)
    evidence_url = _resource_id(upload_body, "url")
    upload_ok = upload.status_code in (200, 201) and bool(evidence_url)
    rows.append(
        _record(
            "M2 · Evidencia de devolución",
            "POST",
            upload_path,
            "200/201 con url",
            upload,
            ok=upload_ok,
            resource_id=evidence_url,
            detail="" if upload_ok else f"code={_error_code(upload_body)}",
        )
    )

    if return_order in owned_ids:
        if return_order == own_order and isinstance(own_body, dict):
            return_detail = own_body
        else:
            detail_path = f"/api/v1/pedidos/{quote(return_order, safe='')}"
            response = client.get(build_url(sales_base, detail_path))
            return_detail = _parse_body(response)
            detail_ok = response.status_code == 200 and isinstance(return_detail, dict)
            rows.append(
                _record(
                    "M1 · Detalle del pedido a devolver",
                    "GET",
                    detail_path,
                    "200 con items",
                    response,
                    ok=detail_ok and bool(build_return_items(return_detail)),
                    resource_id=return_order,
                    detail="" if detail_ok else "No se pudo leer el pedido a devolver.",
                )
            )
    else:
        return_detail = {}
        rows.append(
            _skipped(
                "M1 · Detalle del pedido a devolver",
                "GET",
                f"/api/v1/pedidos/{quote(return_order, safe='')}",
                "200 con items",
                f"{return_order} no aparece en /pedidos/me para este token.",
            )
        )

    return_items = build_return_items(return_detail) if isinstance(return_detail, dict) else []
    if not upload_ok or not return_items:
        rows.append(
            _skipped(
                "M2 · Alta de devolución",
                "POST",
                "/api/v2/devoluciones",
                "201 con devolucionId",
                "Falta evidencia o el pedido de devolución no tiene items.",
            )
        )
        return_id: str | None = None
    else:
        return_payload = build_return_payload(return_order, return_items, evidence_url)
        response = client.post(build_url(postventa_base, "/api/v2/devoluciones"), json=return_payload)
        body = _parse_body(response)
        return_id = _resource_id(body, "devolucionId", "id")
        created = response.status_code == 201 and bool(return_id)
        rows.append(
            _record(
                "M2 · Alta de devolución",
                "POST",
                "/api/v2/devoluciones",
                "201 con devolucionId",
                response,
                ok=created,
                resource_id=return_id,
                detail="" if created else f"code={_error_code(body)}",
            )
        )

    # 8. Listado y detalle de devoluciones.
    list_path = "/api/v2/devoluciones/me?pagina=0&tamano=50"
    response = client.get(
        build_url(postventa_base, "/api/v2/devoluciones/me", {"pagina": 0, "tamano": 50})
    )
    body = _parse_body(response)
    returned = _page_items(body)
    returns_list_ok = response.status_code == 200 and returned is not None
    found = return_id is not None and any(
        str(row.get("devolucionId")) == return_id for row in (returned or []) if isinstance(row, dict)
    )
    rows.append(
        _record(
            "M2 · Listado de devoluciones",
            "GET",
            list_path,
            "200 con items",
            response,
            ok=returns_list_ok,
            resource_id=f"{_page_total(body) if _page_total(body) is not None else len(returned or [])} fila(s)",
            detail=(
                "La lista no tiene items/contenido."
                if not returns_list_ok
                else f"La devolución {return_id} no apareció en la primera página."
                if return_id and not found
                else ""
            ),
        )
    )

    if return_id:
        return_detail_path = f"/api/v2/devoluciones/{quote(return_id, safe='')}"
        response = client.get(build_url(postventa_base, return_detail_path))
        body = _parse_body(response)
        return_detail_ok = (
            response.status_code == 200
            and isinstance(body, dict)
            and str(body.get("devolucionId")) == return_id
        )
        rows.append(
            _record(
                "M2 · Detalle de devolución",
                "GET",
                return_detail_path,
                "200 con el mismo devolucionId",
                response,
                ok=return_detail_ok,
                resource_id=return_id,
                detail="" if return_detail_ok else "El detalle no devolvió el devolucionId creado.",
            )
        )
    else:
        rows.append(
            _skipped(
                "M2 · Detalle de devolución",
                "GET",
                "/api/v2/devoluciones/{id}",
                "200",
                "No hay devolucionId porque el alta no se ejecutó.",
            )
        )

    # 9. Reclamo con los cinco motivos válidos de G5.
    claim_order = own_order if own_order in owned_ids else csat_order
    if claim_order not in owned_ids:
        rows.append(
            _skipped(
                "M2 · Alta de reclamo",
                "POST",
                "/api/v2/reclamos",
                "201 con codigoSeguimiento",
                "Ningún pedido configurado pertenece a este token.",
            )
        )
        claim_code: str | None = None
    else:
        claim_payload = build_claim_payload(claim_order)
        response = client.post(build_url(postventa_base, "/api/v2/reclamos"), json=claim_payload)
        body = _parse_body(response)
        claim_code = _resource_id(body, "codigoSeguimiento", "reclamoId", "id")
        claim_created = response.status_code == 201 and bool(claim_code)
        rows.append(
            _record(
                "M2 · Alta de reclamo",
                "POST",
                "/api/v2/reclamos",
                "201 con codigoSeguimiento",
                response,
                ok=claim_created,
                resource_id=claim_code,
                detail="" if claim_created else f"code={_error_code(body)}",
            )
        )

    claims_list_path = "/api/v2/reclamos/me?pagina=0&tamano=50"
    response = client.get(
        build_url(postventa_base, "/api/v2/reclamos/me", {"pagina": 0, "tamano": 50})
    )
    body = _parse_body(response)
    claims = _page_items(body)
    claims_list_ok = response.status_code == 200 and claims is not None
    rows.append(
        _record(
            "M2 · Listado de reclamos",
            "GET",
            claims_list_path,
            "200 con items",
            response,
            ok=claims_list_ok,
            resource_id=f"{_page_total(body) if _page_total(body) is not None else len(claims or [])} fila(s)",
            detail="" if claims_list_ok else "La lista no tiene items/contenido.",
        )
    )

    if claim_code:
        claim_detail_path = f"/api/v2/reclamos/{quote(claim_code, safe='')}"
        response = client.get(build_url(postventa_base, claim_detail_path))
        body = _parse_body(response)
        claim_detail_ok = (
            response.status_code == 200
            and isinstance(body, dict)
            and str(body.get("codigoSeguimiento") or body.get("reclamoId")) == claim_code
        )
        rows.append(
            _record(
                "M2 · Detalle de reclamo",
                "GET",
                claim_detail_path,
                "200 con el mismo codigoSeguimiento",
                response,
                ok=claim_detail_ok,
                resource_id=claim_code,
                detail="" if claim_detail_ok else "El detalle no devolvió el código creado.",
            )
        )
    else:
        rows.append(
            _skipped(
                "M2 · Detalle de reclamo",
                "GET",
                "/api/v2/reclamos/{codigoSeguimiento}",
                "200",
                "No hay codigoSeguimiento porque el alta no se ejecutó.",
            )
        )

    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evidencia de integración G5 M1/M2 con un token de cliente."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Imprime el plan sin llamadas ni reporte (comportamiento por defecto).",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Ejecuta la prueba viva y escribe docs/evidencia-g5-<UTC>.md.",
    )
    parser.add_argument(
        "--sales-base",
        default=os.getenv("G5_SALES_BASE", DEFAULT_SALES_BASE),
        help="Base de M1 (pedidos).",
    )
    parser.add_argument(
        "--postventa-base",
        default=os.getenv("G5_POSTVENTA_BASE", DEFAULT_POSTVENTA_BASE),
        help="Base de M2 (CSAT, devoluciones, reclamos).",
    )
    parser.add_argument(
        "--gateway-base",
        default="",
        help="Opcional: usa el gateway para ambos servicios (no es el default).",
    )
    parser.add_argument("--own-order", default=DEFAULT_OWN_ORDER)
    parser.add_argument("--foreign-order", default=DEFAULT_FOREIGN_ORDER)
    parser.add_argument("--csat-order", default=DEFAULT_CSAT_ORDER)
    parser.add_argument("--return-order", default=DEFAULT_RETURN_ORDER)
    parser.add_argument("--paid-order", default=DEFAULT_PAID_ORDER)
    parser.add_argument("--docs-dir", default=str(DEFAULT_DOCS_DIR))
    args = parser.parse_args(argv)

    sales_base = normalize_base_url(args.gateway_base or args.sales_base, DEFAULT_SALES_BASE)
    postventa_base = normalize_base_url(args.gateway_base or args.postventa_base, DEFAULT_POSTVENTA_BASE)
    token = (os.getenv("G5_BEARER_TOKEN", "") or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    subject: str | None = None
    if token:
        try:
            subject = extract_subject(token)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if not args.execute:
        print(
            _plan_text(
                sales_base=sales_base,
                postventa_base=postventa_base,
                subject=subject,
                own_order=args.own_order,
                foreign_order=args.foreign_order,
                csat_order=args.csat_order,
                return_order=args.return_order,
                paid_order=args.paid_order,
            )
        )
        return 0

    if not token or subject is None:
        print("ERROR: falta G5_BEARER_TOKEN (token de cliente) para ejecutar la prueba viva.", file=sys.stderr)
        return 2

    now = _utcnow()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers) as client:
            rows = _run_live(
                client=client,
                sales_base=sales_base,
                postventa_base=postventa_base,
                subject=subject,
                own_order=args.own_order,
                foreign_order=args.foreign_order,
                csat_order=args.csat_order,
                return_order=args.return_order,
                paid_order=args.paid_order,
            )
    except httpx.HTTPError as exc:
        rows = [
            CaseEvidence(
                case="Ejecución interrumpida",
                method="—",
                path="—",
                expected="sin errores de red",
                performed_at=_utcnow(),
                result=FAIL,
                detail=f"{type(exc).__name__}: {exc}",
            )
        ]

    report = render_markdown(
        rows,
        sales_base=sales_base,
        postventa_base=postventa_base,
        generated_at=now,
    )
    target = evidence_path(now, args.docs_dir)
    _write_markdown(target, report)

    failures = [row for row in rows if row.result == FAIL]
    for row in rows:
        print(f"{row.result:4} {row.case}: {row.status_code if row.status_code is not None else '—'} {row.detail}".rstrip())
    print(f"\nReporte: {target}")
    print(f"Resultado: {len(rows) - len(failures) - sum(1 for row in rows if row.result == SKIP)}/{len(rows)} OK, {len(failures)} fallo(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
