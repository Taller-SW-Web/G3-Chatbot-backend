"""Unexpected catalog failures never leak the exception class to the customer (#255).

The 503 body carries a constant detail plus the request `correlationId`; the
class name and traceback stay in the internal log so support can join the
customer report with the server evidence.
"""
import logging
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError

from src.modules.mod_catalog.adapters.http.catalog_router import create_catalog_router
from src.modules.mod_catalog.application.manage_catalog_use_case import ManageCatalogUseCase
from src.shared.domain.module_errors import GENERIC_UNAVAILABLE_TEXT
from src.shared.infrastructure.http.correlation import CorrelationMiddleware
from src.shared.infrastructure.http.problem import register_problem_handlers

CORRELATION_ID = str(uuid.uuid4())


def _integrity_error() -> IntegrityError:
    return IntegrityError(
        "INSERT INTO conversation_context ...",
        {},
        Exception("duplicate key value violates unique constraint"),
    )


class ExplodingCatalog:
    """Only the first catalog reads are needed: the search is what explodes."""

    async def list_categories(self):
        return []

    async def list_brands(self):
        return []

    async def search_products(self, **_kwargs):
        raise _integrity_error()


def _use_case() -> ManageCatalogUseCase:
    return ManageCatalogUseCase(
        catalog=ExplodingCatalog(),
        inventory=object(),
        conversation_context=object(),
        uow=object(),
    )


def test_unexpected_error_maps_to_a_generic_detail() -> None:
    error = ManageCatalogUseCase.map_module_error(_integrity_error())
    assert error.code == "SERVICIO_NO_DISPONIBLE"
    assert error.detail == GENERIC_UNAVAILABLE_TEXT
    assert "IntegrityError" not in error.detail


@pytest.mark.asyncio
async def test_unexpected_integrity_error_returns_503_with_a_constant_detail_and_correlation(
    caplog,
) -> None:
    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)
    register_problem_handlers(app)
    app.include_router(create_catalog_router(_use_case), prefix="/api/v1")

    with caplog.at_level(
        logging.ERROR,
        logger="src.modules.mod_catalog.application.manage_catalog_use_case",
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/catalogo/productos",
                headers={"X-Correlation-Id": CORRELATION_ID},
            )

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "SERVICIO_NO_DISPONIBLE"
    assert body["detail"] == GENERIC_UNAVAILABLE_TEXT
    assert body["correlationId"] == CORRELATION_ID
    assert response.headers["X-Correlation-Id"] == CORRELATION_ID
    # The response must not explain internals, but the log must keep the evidence.
    assert "IntegrityError" not in response.text
    assert "IntegrityError" in caplog.text
