"""Correlation middleware contract (issue #181).

Pure ASGI on purpose: the header must reach streamed responses and the body
must never be buffered to add it.
"""
import logging
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.shared.domain.request_context import correlation_id_var
from src.shared.infrastructure.http.correlation import CorrelationMiddleware
from src.shared.infrastructure.http.problem import register_problem_handlers

INCOMING = "123e4567-e89b-12d3-a456-426614174000"
HEADER = "x-correlation-id"


def http_scope(*, path="/api/v1/carrito", headers=None, query_string=b""):
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": list(headers or []),
        "http_version": "1.1",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1234),
    }


async def empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def scripted_app(events, seen=None):
    async def app(scope, receive, send):
        if seen is not None:
            seen.append(correlation_id_var.get())
        for event in events:
            await send(event)
    return app


async def collect(app, scope):
    messages = []

    async def send(message):
        messages.append(message)

    await app(scope, empty_receive, send)
    return messages


def response_header(messages):
    start = next(m for m in messages if m["type"] == "http.response.start")
    values = [value for name, value in start["headers"] if name.lower() == HEADER.encode()]
    assert len(values) == 1
    return values[0].decode()


@pytest.mark.asyncio
async def test_reuses_a_valid_incoming_correlation_id():
    seen = []
    app = CorrelationMiddleware(scripted_app([
        {"type": "http.response.start", "status": 200, "headers": []},
        {"type": "http.response.body", "body": b"ok"},
    ], seen=seen))

    messages = await collect(app, http_scope(headers=[(HEADER.encode(), INCOMING.encode())]))

    assert seen == [INCOMING]
    assert response_header(messages) == INCOMING
    assert correlation_id_var.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [[], [(HEADER.encode(), b"not-a-uuid")]])
async def test_generates_a_uuid_when_missing_or_invalid(headers):
    app = CorrelationMiddleware(scripted_app([
        {"type": "http.response.start", "status": 200, "headers": []},
        {"type": "http.response.body", "body": b""},
    ]))

    messages = await collect(app, http_scope(headers=headers))

    generated = response_header(messages)
    assert uuid.UUID(generated)
    assert generated != "not-a-uuid"


@pytest.mark.asyncio
async def test_replaces_an_upstream_response_header_and_keeps_the_others():
    app = CorrelationMiddleware(scripted_app([
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (HEADER.encode(), b"spoofed"),
                (b"content-type", b"application/json"),
            ],
        },
        {"type": "http.response.body", "body": b"{}"},
    ]))

    messages = await collect(app, http_scope(headers=[(HEADER.encode(), INCOMING.encode())]))

    start = messages[0]
    assert response_header(messages) == INCOMING
    assert (b"content-type", b"application/json") in start["headers"]


@pytest.mark.asyncio
async def test_streaming_response_gets_the_header_without_buffering():
    chunks = [b"data: one\n\n", b"data: two\n\n"]
    app = CorrelationMiddleware(scripted_app([
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        },
        {"type": "http.response.body", "body": chunks[0], "more_body": True},
        {"type": "http.response.body", "body": chunks[1], "more_body": False},
    ]))

    messages = await collect(app, http_scope(headers=[(HEADER.encode(), INCOMING.encode())]))

    assert response_header(messages) == INCOMING
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    assert body == b"".join(chunks)


@pytest.mark.asyncio
async def test_error_response_also_carries_the_header():
    app = CorrelationMiddleware(scripted_app([
        {
            "type": "http.response.start",
            "status": 503,
            "headers": [(b"content-type", b"application/problem+json")],
        },
        {"type": "http.response.body", "body": b'{"code":"MODULO_NO_DISPONIBLE"}'},
    ]))

    messages = await collect(app, http_scope(headers=[(HEADER.encode(), INCOMING.encode())]))

    assert messages[0]["status"] == 503
    assert response_header(messages) == INCOMING


@pytest.mark.asyncio
async def test_logs_method_path_status_duration_and_correlation_without_the_query(caplog):
    caplog.set_level(logging.DEBUG, logger="src.shared.infrastructure.http.correlation")
    app = CorrelationMiddleware(scripted_app([
        {"type": "http.response.start", "status": 200, "headers": []},
        {"type": "http.response.body", "body": b""},
    ]))

    await collect(
        app,
        http_scope(
            path="/api/v1/carrito",
            headers=[(HEADER.encode(), INCOMING.encode())],
            query_string=b"token=super-secret",
        ),
    )

    record = next(
        r for r in caplog.records
        if r.name == "src.shared.infrastructure.http.correlation"
    )
    message = record.getMessage()
    assert "method=GET" in message
    assert "path=/api/v1/carrito" in message
    assert "status=200" in message
    assert "duration_ms=" in message
    assert f"correlation_id={INCOMING}" in message
    assert "super-secret" not in message
    assert record.method == "GET"
    assert record.path == "/api/v1/carrito"
    assert record.http_status == 200
    assert record.duration_ms >= 0
    assert record.correlation_id == INCOMING


@pytest.mark.asyncio
async def test_non_http_scopes_are_untouched():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    await CorrelationMiddleware(app)({"type": "lifespan"}, empty_receive, lambda _: None)

    assert seen == ["lifespan"]


def test_handled_http_errors_include_the_header_end_to_end():
    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    @app.get("/missing")
    async def missing():
        raise HTTPException(status_code=404, detail="not found")

    client = TestClient(app)
    success = client.get("/ok", headers={"X-Correlation-Id": INCOMING})
    assert success.status_code == 200
    assert success.headers[HEADER] == INCOMING

    error = client.get("/missing", headers={"X-Correlation-Id": "invalid"})
    assert error.status_code == 404
    generated = error.headers[HEADER]
    assert uuid.UUID(generated)
    assert generated != "invalid"


def test_unhandled_500_keeps_the_plain_body_but_adds_the_header():
    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)
    register_problem_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom", headers={"X-Correlation-Id": INCOMING})

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers[HEADER] == INCOMING
    assert "boom" not in response.text
