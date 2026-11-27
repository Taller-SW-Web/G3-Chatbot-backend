"""HTTP healthcheck of the consumer process (ADR-0030)."""
import asyncio
import json

import pytest
import pytest_asyncio

from src.shared.infrastructure.broker.health_server import HealthServer


async def _request(port: int, request: bytes) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    return status, json.loads(body) if body else {}


@pytest_asyncio.fixture
async def server():
    state = {"healthy": True}
    started = HealthServer(
        0,
        lambda: (state["healthy"], {"status": "ok" if state["healthy"] else "unavailable", "counters": {"consumed": 3}}),
        host="127.0.0.1",
    )
    await started.start()
    yield started, state
    await started.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/", "/health?probe=1"])
async def test_reports_200_with_the_counters_while_healthy(server, path):
    started, _ = server
    status, body = await _request(started.port, f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert status == 200
    assert body == {"status": "ok", "counters": {"consumed": 3}}


@pytest.mark.asyncio
async def test_reports_503_when_the_consumer_is_not_healthy(server):
    started, state = server
    state["healthy"] = False
    status, body = await _request(started.port, b"GET /health HTTP/1.1\r\n\r\n")
    assert status == 503 and body["status"] == "unavailable"


@pytest.mark.asyncio
async def test_unknown_paths_and_methods_are_rejected(server):
    started, _ = server
    assert (await _request(started.port, b"GET /admin HTTP/1.1\r\n\r\n"))[0] == 404
    assert (await _request(started.port, b"POST /health HTTP/1.1\r\n\r\n"))[0] == 405


@pytest.mark.asyncio
async def test_a_garbage_request_does_not_kill_the_server(server):
    started, _ = server
    reader, writer = await asyncio.open_connection("127.0.0.1", started.port)
    writer.write(b"\r\n")
    await writer.drain()
    assert await reader.read() == b""
    writer.close()
    assert (await _request(started.port, b"GET /health HTTP/1.1\r\n\r\n"))[0] == 200


@pytest.mark.asyncio
async def test_stop_releases_the_port():
    started = HealthServer(0, lambda: (True, {}), host="127.0.0.1")
    await started.start()
    port = started.port
    await started.stop()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
