"""SPEC-06 Req. 4 · Scenarios «Más de 10 resultados» and «Sin resultados».

The carousel carries `datos.total` and `datos.siguiente`, a bare page turn
reuses `filtrosVigentes`, and an empty search offers valid relaxations.
"""
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.mod_catalog.adapters.http.catalog_router import create_catalog_router
from src.modules.mod_catalog.application.tools import SearchArguments, build_tool_registry
from src.modules.mod_chat.application.tools.action_dispatcher import ActionDispatcher
from src.shared.application.tools.tool_registry import ExecutionContext, ToolResult
from tests.unit.mod_catalog.test_catalog_use_case import (
    RecordingCatalog,
    customer_context,
    make_catalog,
    owned_catalog,
)
from tests.unit.mod_catalog.test_offer_discovery_review import paged


@pytest.mark.asyncio
async def test_more_than_10_results_shows_10_total_and_next_page():
    port = RecordingCatalog()
    catalog = owned_catalog(make_catalog(catalog=port))

    result = await catalog.search(
        SearchArguments(q="zapatillas"), customer_context()
    )

    datos = result["bloques"][0]["datos"]
    assert len(datos["productos"]) == 10
    assert datos["total"] == 34
    assert datos["siguiente"] == {
        "pagina": 2,
        "q": None,
        "categoriaId": "cat-zapatillas",
        "marcaId": None,
        "precioMin": None,
        "precioMax": None,
        "orden": None,
        "soloOfertas": None,
    }
    assert port.search_calls[0]["page"] == 1


@pytest.mark.asyncio
async def test_next_page_payload_carries_normalized_filters():
    port = RecordingCatalog()
    catalog = owned_catalog(make_catalog(catalog=port))

    result = await catalog.search(
        SearchArguments(q="zapatillas", marca="Nike", precioMax=500, orden="precio_desc"),
        customer_context(),
    )

    datos = result["bloques"][0]["datos"]
    assert len(datos["productos"]) == 10
    assert datos["total"] == 12
    assert datos["siguiente"] == {
        "pagina": 2,
        "q": None,
        "categoriaId": "cat-zapatillas",
        "marcaId": "brand-nike",
        "precioMin": None,
        "precioMax": 500.0,
        "orden": "precio_desc",
        "soloOfertas": None,
    }


@pytest.mark.asyncio
async def test_page_two_reuses_normalized_filters_from_conversation():
    port = RecordingCatalog()
    catalog = owned_catalog(make_catalog(catalog=port))
    first = await catalog.search(SearchArguments(q="zapatillas"), customer_context())

    result = await catalog.search(SearchArguments(pagina=2), customer_context())

    sent = port.search_calls[-1]
    assert sent["page"] == 2
    assert sent["q"] is None
    assert sent["category_id"] == "cat-zapatillas"
    assert sent["brand_id"] is None
    datos = result["bloques"][0]["datos"]
    assert len(datos["productos"]) == 10
    assert datos["total"] == 34
    assert datos["siguiente"]["pagina"] == 3
    assert datos["siguiente"]["categoriaId"] == "cat-zapatillas"
    first_page = {row["product_id"] for row in first["bloques"][0]["datos"]["productos"]}
    assert first_page.isdisjoint({row["product_id"] for row in datos["productos"]})


@pytest.mark.asyncio
async def test_last_page_has_no_next_page():
    catalog, context = paged(total=34)

    result = await catalog.search(SearchArguments(pagina=4), context)

    datos = result["bloques"][0]["datos"]
    assert len(datos["productos"]) == 4
    assert datos["total"] == 34
    assert datos["siguiente"] is None


@pytest.mark.asyncio
async def test_variant_hint_pages_do_not_offer_a_payload_that_drops_it():
    catalog, context = paged(total=34)

    result = await catalog.search(SearchArguments(talla="42"), context)

    datos = result["bloques"][0]["datos"]
    assert datos["total"] == len(datos["productos"])
    assert datos["siguiente"] is None


@pytest.mark.asyncio
async def test_empty_results_suggest_up_to_three_normalized_relaxations():
    catalog = owned_catalog(make_catalog(products=[]))

    result = await catalog.search(
        SearchArguments(q="zapatillas", marca="Nike", precioMax=300),
        customer_context(),
    )

    assert result["total"] == 0
    assert [bloque["tipo"] for bloque in result["bloques"]] == ["ACCIONES_RAPIDAS"]
    acciones = result["bloques"][0]["datos"]["acciones"]
    assert [accion["etiqueta"] for accion in acciones] == [
        "Buscar sin precio máximo",
        "Buscar sin la marca Nike",
        "Buscar sin la categoría Zapatillas",
    ]
    assert all(accion["id"] == "BUSCAR_PRODUCTOS" for accion in acciones)
    assert acciones[0]["payload"] == {
        "categoriaId": "cat-zapatillas", "marcaId": "brand-nike",
    }
    assert acciones[1]["payload"] == {
        "categoriaId": "cat-zapatillas", "precioMax": 300.0,
    }
    assert acciones[2]["payload"] == {
        "marcaId": "brand-nike", "precioMax": 300.0,
    }
    # Every relaxation is a valid tool payload with normalized filters only.
    for accion in acciones:
        SearchArguments(**accion["payload"])


@pytest.mark.asyncio
async def test_buscar_pagina_action_dispatches_the_page_to_the_search_handler():
    handler = AsyncMock(return_value=ToolResult("OK", {}))
    registry = build_tool_registry({"buscar_productos": handler})
    context = ExecutionContext("conv-1", None, "anon-1")

    result = await ActionDispatcher(registry).despachar(
        {"tipo": "BUSCAR_PAGINA", "payload": {"pagina": 2}}, context,
    )

    assert result.code == "OK"
    argumentos, recibido = handler.await_args.args
    assert argumentos.pagina == 2
    assert recibido is context


def test_rest_empty_search_returns_no_items_and_relaxations():
    catalog = owned_catalog(make_catalog(products=[]))
    app = FastAPI()
    app.include_router(create_catalog_router(lambda: catalog))

    with TestClient(app) as client:
        response = client.get(
            "/catalogo/productos",
            params={"q": "zapatillas", "marca": "Nike", "precioMax": 300},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert "No encontré productos" in body["mensaje"]
