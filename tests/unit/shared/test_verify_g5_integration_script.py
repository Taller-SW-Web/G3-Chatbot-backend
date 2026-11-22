"""Unit tests for scripts/verify_g5_integration.py (no network, no secrets)."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from scripts.verify_g5_integration import (
    DEFAULT_CSAT_ORDER,
    DEFAULT_FOREIGN_ORDER,
    DEFAULT_OWN_ORDER,
    DEFAULT_PAID_ORDER,
    DEFAULT_POSTVENTA_BASE,
    DEFAULT_RETURN_ORDER,
    DEFAULT_SALES_BASE,
    CaseEvidence,
    build_claim_payload,
    build_csat_payload,
    build_return_items,
    build_return_payload,
    build_url,
    decode_jwt_payload,
    evidence_path,
    extract_subject,
    format_utc,
    main,
    normalize_base_url,
    probe_health,
    render_markdown,
    snippet,
)

SUBJECT = "be1f271e-bb33-4905-a552-554bbe46f2fe"


def _jwt(claims: dict, *, signature: str = "firma-no-verificada") -> str:
    def part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'RS256', 'typ': 'JWT'})}.{part(claims)}.{signature}"


def test_defaults_and_url_helpers() -> None:
    assert normalize_base_url(None, DEFAULT_SALES_BASE) == DEFAULT_SALES_BASE
    assert normalize_base_url("https://m1.test///", DEFAULT_SALES_BASE) == "https://m1.test"
    assert build_url("https://m1.test/", "/api/v1/pedidos", {"page": 1, "desde": None}) == (
        "https://m1.test/api/v1/pedidos?page=1"
    )
    assert DEFAULT_POSTVENTA_BASE.startswith("https://g5-ventas-postventas")
    assert DEFAULT_OWN_ORDER == "PED-2026-90201"
    assert DEFAULT_CSAT_ORDER == "PED-2026-90201"
    assert DEFAULT_RETURN_ORDER == "PED-2026-90201"
    assert DEFAULT_FOREIGN_ORDER == "PED-2026-90101"
    assert DEFAULT_PAID_ORDER == "PED-2026-90202"


def test_decode_jwt_payload_and_subject_without_signature() -> None:
    token = _jwt({"sub": SUBJECT, "roles": ["CLIENTE"]})

    assert decode_jwt_payload(token)["sub"] == SUBJECT
    assert extract_subject(token) == SUBJECT


@pytest.mark.parametrize("token", ["", "no-es-un-jwt", "a.b", _jwt({"roles": ["CLIENTE"]})])
def test_extract_subject_rejects_malformed_tokens(token: str) -> None:
    with pytest.raises(ValueError):
        extract_subject(token)


def test_snippet_redacts_token_and_personal_data() -> None:
    text = snippet(
        {
            "access_token": "token-secreto",
            "clienteId": SUBJECT,
            "consumidor": {"documento": "72458912", "email": "ana@example.com"},
            "devolucionId": "DEV-2026-0042",
            "nota": "linea 1\nlinea 2 | con pipe",
        }
    )

    assert "token-secreto" not in text
    assert SUBJECT not in text
    assert "72458912" not in text
    assert "ana@example.com" not in text
    assert "DEV-2026-0042" in text
    assert "\\|" in text
    assert len(snippet("x" * 500)) == 280


def test_payloads_use_the_g5_contract() -> None:
    csat = build_csat_payload("PED-2026-90201", SUBJECT)
    assert csat["clienteId"] == SUBJECT
    assert csat["canal"] == "CHATBOT"
    assert csat["puntuacion"] == 5

    items = build_return_items(
        {"items": [{"productoId": "P-1", "cantidad": 2}, {"cantidad": 1}, {"productoId": "P-2"}]}
    )
    assert items == [{"productoId": "P-1", "cantidad": 2}, {"productoId": "P-2", "cantidad": 1}]

    ret = build_return_payload("PED-2026-90201", items, "https://m2.test/ev.png")
    assert ret["motivo"] == "OTRO"
    assert ret["tipo"] == "DEVOLUCION_DINERO"
    assert ret["evidencias"] == [{"tipo": "IMAGEN", "url": "https://m2.test/ev.png"}]

    claim = build_claim_payload("PED-2026-90101")
    assert claim["motivo"] == "INCUMPLIMIENTO_PLAZO_ENTREGA"
    assert claim["canal"] == "CHATBOT"
    assert len(claim["detalle"]) >= 20


def test_evidence_path_is_utc_with_seconds(tmp_path) -> None:
    now = datetime(2026, 11, 22, 15, 30, 1, tzinfo=timezone.utc)
    assert evidence_path(now, tmp_path).name == "evidencia-g5-20261122-153001Z.md"
    assert evidence_path(
        datetime(2026, 11, 22, 10, 30, 1, tzinfo=timezone(timedelta(hours=-5))), tmp_path
    ).name == "evidencia-g5-20261122-153001Z.md"
    assert format_utc(now) == "2026-11-22T15:30:01Z"


def test_render_markdown_contains_the_table_and_failures() -> None:
    now = datetime(2026, 11, 22, 15, 30, 1, tzinfo=timezone.utc)
    rows = [
        CaseEvidence(
            case="M1 · Listado",
            method="GET",
            path="/api/v1/pedidos/me",
            expected="200",
            performed_at=now,
            status_code=200,
            result="OK",
            body_snippet='{"total":2}',
        ),
        CaseEvidence(
            case="M2 · CSAT",
            method="POST",
            path="/api/v2/csat",
            expected="201",
            performed_at=now,
            status_code=409,
            result="FAIL",
            detail="code=CSAT_ALREADY_SUBMITTED",
        ),
    ]

    report = render_markdown(
        rows, sales_base="https://m1.test", postventa_base="https://m2.test", generated_at=now
    )

    assert "M1 (pedidos)" in report
    assert "M2 (postventa)" in report
    assert "**1/2 OK**, 1 fallo(s)" in report
    assert "code=CSAT_ALREADY_SUBMITTED" in report
    assert "Ninguno." not in report


def test_probe_health_reads_up_and_failure() -> None:
    up = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "UP"})))
    down = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503, text="starting")))

    assert probe_health(up, "https://m1.test") == (True, "HTTP 200 status=UP")
    healthy, detail = probe_health(down, "https://m1.test")
    assert healthy is False and "503" in detail


def test_dry_run_is_default_and_prints_both_services(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("G5_BEARER_TOKEN", _jwt({"sub": SUBJECT}))
    monkeypatch.setenv("G5_SALES_BASE", "https://m1.test")
    monkeypatch.setenv("G5_POSTVENTA_BASE", "https://m2.test")

    code = main(["--dry-run", "--docs-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "https://m1.test" in out
    assert "https://m2.test" in out
    assert SUBJECT in out
    assert "Cuenta por defecto: 2" in out
    assert "PED-2026-90201" in out
    assert "PED-2026-90101" in out
    assert "PED-2026-90202" in out
    assert "POST /api/v2/devoluciones" in out
    assert list(tmp_path.iterdir()) == []


def test_dry_run_without_token_still_prints_a_plan(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("G5_BEARER_TOKEN", raising=False)

    code = main(["--docs-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "ausente" in out
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("args", [["--execute"], ["--dry-run"]])
def test_missing_or_invalid_token_is_a_configuration_error(tmp_path, monkeypatch, capsys, args) -> None:
    monkeypatch.setenv("G5_BEARER_TOKEN", "no-es-un-jwt")

    code = main([*args, "--docs-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 2
    assert "JWT" in captured.err
    assert "no-es-un-jwt" not in captured.out
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_execute_writes_a_full_m1_m2_report(tmp_path, monkeypatch, capsys) -> None:
    token = _jwt({"sub": SUBJECT, "roles": ["CLIENTE"]})
    monkeypatch.setenv("G5_BEARER_TOKEN", token)
    m1, m2 = "https://m1.test", "https://m2.test"
    own, foreign, csat, ret, paid = (
        "PED-2026-90101",
        "PED-2026-99999",
        "PED-2026-90201",
        "PED-2026-90201",
        "PED-2026-90103",
    )
    orders = [
        {"pedidoId": own, "estado": "ENTREGADO", "total": 10.0, "moneda": "PEN", "canal": "CHATBOT", "fechaCreacion": "2026-11-10T10:00:00Z"},
        {"pedidoId": csat, "estado": "ENTREGADO", "total": 20.0, "moneda": "PEN", "canal": "CHATBOT", "fechaCreacion": "2026-11-10T10:00:00Z"},
        {"pedidoId": paid, "estado": "PAGADO", "total": 30.0, "moneda": "PEN", "canal": "CHATBOT", "fechaCreacion": "2026-11-10T10:00:00Z"},
    ]
    csat_calls: dict[str, int] = {}

    def list_orders(request):
        if request.url.params.get("desde") == "2030-01-01":
            return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "pageSize": 50})
        rows = [row for row in orders if row["estado"] == "ENTREGADO"] if request.url.params.get("estado") else orders
        return httpx.Response(200, json={"items": rows, "total": len(rows), "page": 1, "pageSize": 50})

    def detail(request):
        order_id = request.url.path.rsplit("/", 1)[-1]
        if order_id == foreign:
            return httpx.Response(403, json={"codigo": "ACCESO_DENEGADO"})
        return httpx.Response(
            200,
            json={
                "pedidoId": order_id,
                "estado": "ENTREGADO",
                "items": [{"productoId": "P-1", "sku": "SKU-1", "cantidad": 2, "precioUnitario": 5.0}],
                "historialEstados": [{"estado": "ENTREGADO", "fecha": "2026-11-11T10:00:00Z"}],
            },
        )

    def create_csat(request):
        body = json.loads(request.content)
        order_id = body["pedidoId"]
        if order_id == paid:
            return httpx.Response(422, json={"codigo": "ORDER_NOT_DELIVERED"})
        csat_calls[order_id] = csat_calls.get(order_id, 0) + 1
        if csat_calls[order_id] == 1:
            return httpx.Response(201)
        return httpx.Response(409, json={"codigo": "CSAT_ALREADY_SUBMITTED"})

    respx.get(f"{m1}/actuator/health").mock(return_value=httpx.Response(200, json={"status": "UP"}))
    respx.get(f"{m2}/actuator/health").mock(return_value=httpx.Response(200, json={"status": "UP"}))
    respx.get(f"{m1}/api/v1/pedidos/me").mock(side_effect=list_orders)
    respx.get(f"{m1}/api/v1/pedidos/{own}").mock(side_effect=detail)
    respx.get(f"{m1}/api/v1/pedidos/{ret}").mock(side_effect=detail)
    respx.get(f"{m1}/api/v1/pedidos/{foreign}").mock(side_effect=detail)
    respx.post(f"{m2}/api/v2/csat").mock(side_effect=create_csat)
    respx.post(f"{m2}/api/v2/devoluciones/evidencias/upload").mock(
        return_value=httpx.Response(
            201,
            json={"tipo": "IMAGEN", "url": "https://m2.test/ev.png", "nombreArchivoOriginal": "e.png", "tamanioBytes": 10, "fechaSubida": "x"},
        )
    )
    respx.post(f"{m2}/api/v2/devoluciones").mock(
        return_value=httpx.Response(201, json={"devolucionId": "DEV-TEST-1", "pedidoId": ret, "estado": "SOLICITADA"})
    )
    respx.get(f"{m2}/api/v2/devoluciones/me").mock(
        return_value=httpx.Response(200, json={"contenido": [{"devolucionId": "DEV-TEST-1"}], "totalElementos": 1})
    )
    respx.get(f"{m2}/api/v2/devoluciones/DEV-TEST-1").mock(
        return_value=httpx.Response(200, json={"devolucionId": "DEV-TEST-1", "pedidoId": ret, "estado": "SOLICITADA"})
    )
    respx.post(f"{m2}/api/v2/reclamos").mock(
        return_value=httpx.Response(201, json={"reclamoId": "REC-TEST-1", "codigoSeguimiento": "REC-TEST-1"})
    )
    respx.get(f"{m2}/api/v2/reclamos/me").mock(
        return_value=httpx.Response(200, json={"contenido": [{"codigoSeguimiento": "REC-TEST-1"}], "totalElementos": 1})
    )
    respx.get(f"{m2}/api/v2/reclamos/REC-TEST-1").mock(
        return_value=httpx.Response(200, json={"reclamoId": "REC-TEST-1", "codigoSeguimiento": "REC-TEST-1"})
    )

    code = main(
        [
            "--execute",
            "--sales-base", m1,
            "--postventa-base", m2,
            "--own-order", own,
            "--foreign-order", foreign,
            "--csat-order", csat,
            "--return-order", ret,
            "--paid-order", paid,
            "--docs-dir", str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    reports = list(tmp_path.glob("evidencia-g5-*.md"))
    assert code == 0, captured.err + captured.out
    assert csat_calls[csat] == 2
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert "Ninguno." in report
    assert "DEV-TEST-1" in report
    assert "REC-TEST-1" in report
    assert token not in report
    assert "72458912" not in report
