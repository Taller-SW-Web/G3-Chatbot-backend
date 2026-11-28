"""H5-E1 reference conversations: real OpenAI provider, fake modules, assertions on tools/data.

Each case exercises the production agent loop (`InterpretAndRespondUseCase`)
against the real LLM and the real tool registry. Handlers are wired to the
deterministic seed catalog and in-memory carts, so no case needs a database or
network access to G6/G7. Text is only inspected to prove that no amount outside
the tool results leaked into the reply; the main assertions are on tool names,
arguments and returned blocks.

Without `LLM_API_KEY` the seven live cases skip with an explicit message; the
degraded-mode case, the degraded-turn guardrail and the harness smoke test still
run offline.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

from src.modules.mod_chat.adapters.openai_nlp_adapter import OpenAILLMProvider
from src.modules.mod_chat.application.dto.conversation_events import (
    BlockEvent,
    DegradedEvent,
    FinalEvent,
    MessageAcceptedEvent,
)
from src.modules.mod_chat.domain.output_validator import extract_pen_amounts
from src.modules.mod_chat.ports.llm_provider_port import (
    Completed,
    LLMRole,
    LLMUnavailableError,
    TextDelta,
    TextPart,
    ToolCall,
    ToolCallRequested,
)
from src.modules.mod_chat.ports.tool_executor_port import ToolResult
from tests.evals.harness import (
    EvalCase,
    ShoppingWorld,
    ToolCallRecord,
    assert_no_amounts,
    assert_no_invented_amounts,
    block_types,
    final_text,
    run_reference_turn,
)
from tests.fakes.fake_llm_provider import FakeLLMProvider
from tests.fakes.fake_tool_executor import FakeToolExecutor

pytestmark = pytest.mark.evals

CARD = "4111 1111 1111 1111"
PASSWORD = "Secreta123!"


def _require_llm_key() -> str:
    key = os.getenv("LLM_API_KEY", "").strip()
    if not key:
        pytest.skip("LLM_API_KEY no configurada")
    return key


def _real_world() -> ShoppingWorld:
    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    provider = OpenAILLMProvider(
        api_key=_require_llm_key(),
        model=os.getenv("LLM_MODEL", "gpt-6-luna").strip() or "gpt-6-luna",
        temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
        max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1024")),
        timeout_seconds=timeout,
    )
    return ShoppingWorld(provider, timeout_seconds=timeout)


def _successful(world: ShoppingWorld, name: str) -> list[ToolCallRecord]:
    return [call for call in world.tools.named(name) if call.result.code == "OK"]


def _blocks_of(call: ToolCallRecord, kind: str) -> list[dict[str, Any]]:
    return [block for block in call.result.blocks if block.get("tipo") == kind]


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@pytest.mark.asyncio
async def test_search_reference_conversation(eval_case: EvalCase):
    """busqueda-filtrado · Búsqueda: el modelo usa buscar_productos con palabras clave y la talla, y recibe el carrusel."""
    world = _real_world()

    events = await run_reference_turn(eval_case, world, "Busco zapatillas para correr talla 42")

    calls = _successful(world, "buscar_productos")
    eval_case.check("llamó a buscar_productos", bool(calls), detail=world.tools.names)
    if calls:
        queries = [str(call.arguments.get("q") or "") for call in calls]
        eval_case.check(
            "la búsqueda usa palabras clave del producto",
            any("zapatilla" in query.casefold() or "running" in query.casefold() for query in queries),
            detail=queries,
        )
        eval_case.check(
            "la búsqueda no lleva precios en q",
            all("s/" not in query.casefold() and "soles" not in query.casefold() for query in queries),
            detail=queries,
        )
        eval_case.check(
            "la talla viaja en su propio argumento",
            any(str(call.arguments.get("talla") or "") == "42" for call in calls),
            detail=[call.arguments for call in calls],
        )
        carousel_calls = [
            call for call in calls if _blocks_of(call, "CARRUSEL_PRODUCTOS")
        ]
        eval_case.check(
            "devolvió el carrusel de productos",
            bool(carousel_calls),
            detail=[block.get("tipo") for call in calls for block in call.result.blocks],
        )
        if carousel_calls:
            carousels = _blocks_of(carousel_calls[0], "CARRUSEL_PRODUCTOS")
            products = carousels[0].get("datos", {}).get("productos", [])
            eval_case.check("el carrusel tiene productos", bool(products), detail=len(products))

    assert_no_invented_amounts(eval_case, world, final_text(events))


@pytest.mark.asyncio
async def test_recommendation_reference_conversation(eval_case: EvalCase):
    """recomendacion · Recomendación: usa recomendar_productos con actividad y presupuesto, sin inventar datos."""
    world = _real_world()

    events = await run_reference_turn(
        eval_case,
        world,
        "Quiero empezar a correr y tengo un presupuesto de 300 soles. ¿Qué me recomiendas?",
    )

    calls = _successful(world, "recomendar_productos")
    eval_case.check("llamó a recomendar_productos", bool(calls), detail=world.tools.names)
    if calls:
        activities = [str(call.arguments.get("actividad") or "").casefold() for call in calls]
        eval_case.check(
            "indicó la actividad",
            any(any(word in activity for word in ("correr", "running", "trotar")) for activity in activities),
            detail=[call.arguments for call in calls],
        )
        budget_calls = [
            call for call in calls if _number(call.arguments.get("presupuestoMax")) == 300.0
        ]
        eval_case.check(
            "respetó el presupuesto",
            bool(budget_calls),
            detail=[call.arguments for call in calls],
        )
        target = budget_calls[0] if budget_calls else calls[0]
        carousels = [
            block for block in _blocks_of(target, "CARRUSEL_PRODUCTOS")
            if block.get("datos", {}).get("variant") == "recomendacion"
        ]
        eval_case.check(
            "devolvió recomendaciones estructuradas",
            bool(carousels),
            detail=[block.get("tipo") for block in target.result.blocks],
        )
        if carousels:
            products = carousels[0].get("datos", {}).get("productos", [])
            eval_case.check("la recomendación tiene productos", bool(products), detail=len(products))
            prices = [price for price in (_number(row.get("price")) for row in products) if price is not None]
            eval_case.check(
                "las recomendaciones caben en el presupuesto",
                all(price <= 300.0 for price in prices),
                detail=prices,
            )

    assert_no_invented_amounts(eval_case, world, final_text(events))


@pytest.mark.asyncio
async def test_offers_reference_conversation(eval_case: EvalCase):
    """ofertas-promociones · Ofertas: consulta promociones vigentes y no inventa códigos de cupón."""
    world = _real_world()

    events = await run_reference_turn(eval_case, world, "¿Qué ofertas hay hoy?")
    text = final_text(events)

    calls = _successful(world, "consultar_promociones")
    eval_case.check("llamó a consultar_promociones", bool(calls), detail=world.tools.names)
    if calls:
        kinds = [block.get("tipo") for block in calls[0].result.blocks]
        eval_case.check("devolvió la lista de promociones", "LISTA_PROMOCIONES" in kinds, detail=kinds)
        promotions = next(
            (
                block.get("datos", {}).get("promociones", [])
                for block in calls[0].result.blocks
                if block.get("tipo") == "LISTA_PROMOCIONES"
            ),
            [],
        )
        eval_case.check("las promociones vienen de la herramienta", bool(promotions), detail=promotions)

    coupons = set(re.findall(r"\b[A-Z]{2,}\d{2,}\b", text))
    eval_case.check("no inventó códigos de cupón", not coupons, detail=sorted(coupons))
    assert_no_invented_amounts(eval_case, world, text)


@pytest.mark.asyncio
async def test_cart_reference_conversation(eval_case: EvalCase):
    """gestion-carrito · Carrito: ver_carrito devuelve la línea sembrada y sus totales reales."""
    world = _real_world()
    await world.seed_cart()

    events = await run_reference_turn(eval_case, world, "¿Qué tengo en mi carrito?")

    calls = _successful(world, "ver_carrito")
    eval_case.check("llamó a ver_carrito", bool(calls), detail=world.tools.names)
    if calls:
        carts = _blocks_of(calls[0], "CARRITO")
        eval_case.check(
            "devolvió el bloque CARRITO",
            bool(carts),
            detail=[block.get("tipo") for block in calls[0].result.blocks],
        )
        if carts:
            data = carts[0].get("datos", {})
            lines = data.get("lineas", [])
            eval_case.check(
                "el carrito tiene la línea sembrada",
                len(lines) == 1 and lines[0].get("cantidad") == 2,
                detail=lines,
            )
            subtotal = sum(
                (_number(line.get("precio")) or 0.0) * int(line.get("cantidad") or 0)
                for line in lines
            )
            discounts = sum(
                (_number(discount.get("monto")) or 0.0)
                for discount in data.get("descuentos", [])
            )
            eval_case.check(
                "el total viene del carrito",
                _number(data.get("total")) == round(subtotal - discounts, 2),
                detail={"total": data.get("total"), "subtotal": subtotal, "descuentos": discounts},
            )

    assert_no_invented_amounts(eval_case, world, final_text(events))


@pytest.mark.asyncio
async def test_prices_are_only_quoted_from_tool_data(eval_case: EvalCase):
    """tarjetas-detalle-producto · Precios: el detalle devuelve el precio y el texto no introduce importes ajenos a la herramienta."""
    world = _real_world()

    events = await run_reference_turn(eval_case, world, "¿Cuánto cuesta la Nike Revolution 7?")
    text = final_text(events)

    calls = _successful(world, "ver_detalle_producto")
    eval_case.check("llamó a ver_detalle_producto", bool(calls), detail=world.tools.names)
    if calls:
        detail_calls = [
            call for call in calls
            if any(
                block.get("datos", {}).get("producto", {}).get("name") == "Nike Revolution 7"
                for block in _blocks_of(call, "DETALLE_PRODUCTO")
            )
        ]
        eval_case.check(
            "devolvió el detalle estructurado",
            bool(detail_calls),
            detail=[block.get("tipo") for call in calls for block in call.result.blocks],
        )
        if detail_calls:
            details = _blocks_of(detail_calls[0], "DETALLE_PRODUCTO")
            product = details[0].get("datos", {}).get("producto", {})
            eval_case.check(
                "el precio del detalle es el del seed",
                _number(product.get("price")) == 279.9,
                detail=product,
            )

    assert_no_invented_amounts(eval_case, world, text)
    eval_case.check(
        "respondió con un precio respaldado",
        bool(extract_pen_amounts(text)),
        detail=text,
    )


@pytest.mark.asyncio
async def test_out_of_domain_request_uses_no_tools(eval_case: EvalCase):
    """motor-conversacion · Fuera de dominio: una tarea ajena no dispara herramientas ni datos comerciales."""
    world = _real_world()

    events = await run_reference_turn(
        eval_case, world, "¿Me ayudas a resolver mi tarea de historia universal?"
    )
    text = final_text(events)

    eval_case.check("no llamó herramientas", not world.tools.calls, detail=world.tools.names)
    eval_case.check("respondió algo", bool(text.strip()), detail=text)
    assert_no_amounts(eval_case, text)


@pytest.mark.asyncio
async def test_sensitive_data_is_redacted_before_the_llm(eval_case: EvalCase):
    """motor-conversacion · Datos sensibles: tarjeta y contraseña se redactan antes de persistir y de llamar al LLM."""
    world = _real_world()

    events = await run_reference_turn(
        eval_case,
        world,
        f"Mi tarjeta es {CARD} y mi contraseña es {PASSWORD}. ¿Puedes guardarla?",
    )

    stored = str(world.messages.rows[0]["text"])
    eval_case.check("no persistió la tarjeta", "4111" not in stored, detail=stored)
    eval_case.check("no persistió la contraseña", PASSWORD not in stored, detail=stored)
    eval_case.check("marcó la tarjeta como oculta", "[tarjeta oculta]" in stored, detail=stored)
    eval_case.check("marcó la contraseña como oculta", "[contraseña oculta]" in stored, detail=stored)

    if world.llm.calls:
        sent = " ".join(
            part.text
            for message in world.llm.calls[0][0]
            if message.role is LLMRole.USER
            for part in message.content
            if isinstance(part, TextPart)
        )
        eval_case.check("el LLM no recibió la tarjeta", "4111" not in sent, detail=sent)
        eval_case.check("el LLM no recibió la contraseña", PASSWORD not in sent, detail=sent)

    tool_arguments = json.dumps([call.arguments for call in world.tools.calls], default=str, ensure_ascii=False)
    eval_case.check("ninguna herramienta recibió la tarjeta", "4111" not in tool_arguments, detail=tool_arguments)
    eval_case.check("ninguna herramienta recibió la contraseña", PASSWORD not in tool_arguments, detail=tool_arguments)

    text = final_text(events)
    eval_case.check("la respuesta no repite la tarjeta", "4111" not in text, detail=text)
    eval_case.check("la respuesta no repite la contraseña", PASSWORD not in text, detail=text)


@pytest.mark.asyncio
async def test_degraded_mode_searches_without_the_llm(eval_case: EvalCase):
    """motor-conversacion · Modo degradado: sin LLM, el menú seguro busca por palabra clave y no inventa importes."""
    world = ShoppingWorld(FakeLLMProvider([LLMUnavailableError("simulated outage")]), timeout_seconds=5.0)

    events = await run_reference_turn(eval_case, world, "busco zapatillas", allow_degraded=True)

    eval_case.check(
        "emitió el evento degradado",
        any(isinstance(event, DegradedEvent) and event.reason == "unavailable" for event in events),
        detail=[type(event).__name__ for event in events],
    )
    calls = _successful(world, "buscar_productos")
    eval_case.check("ejecutó la búsqueda de respaldo", bool(calls), detail=world.tools.names)
    if calls:
        eval_case.check(
            "la consulta degradada usa la palabra clave",
            calls[0].arguments.get("q") == "zapatillas",
            detail=calls[0].arguments,
        )
        eval_case.check(
            "devolvió el carrusel de respaldo",
            bool(_blocks_of(calls[0], "CARRUSEL_PRODUCTOS")),
            detail=[block.get("tipo") for block in calls[0].result.blocks],
        )
    eval_case.check(
        "mostró el menú de acciones rápidas",
        "ACCIONES_RAPIDAS" in block_types(events),
        detail=block_types(events),
    )
    assert_no_invented_amounts(eval_case, world, final_text(events))


@pytest.mark.asyncio
async def test_harness_fails_a_live_case_when_the_turn_degrades():
    """Harness · Guardarraíl: un caso real que cae al menú degradado no puede reportarse aprobado."""
    world = ShoppingWorld(FakeLLMProvider([LLMUnavailableError("simulated outage")]), timeout_seconds=5.0)
    case = EvalCase(name="guardrail", scenario="un caso real nunca pasa en modo degradado")

    with pytest.raises(AssertionError, match="modo degradado"):
        await run_reference_turn(case, world, "¿Me ayudas con mi tarea de historia universal?")

    assert case.degraded_reason == "unavailable"
    assert any(not check["passed"] for check in case.assertions)


@pytest.mark.asyncio
async def test_harness_smoke_with_fake_provider(eval_case: EvalCase):
    """Harness · Humo: el harness registra herramienta, bloque, texto y aserciones sin LLM_API_KEY."""
    block = {"id": "p1", "tipo": "CARRUSEL_PRODUCTOS", "datos": {"productos": []}}
    tools = FakeToolExecutor({"buscar_productos": ToolResult("OK", {"texto": "Dos opciones"}, blocks=(block,))})
    llm = FakeLLMProvider([
        [ToolCallRequested(ToolCall("c1", "buscar_productos", {"q": "zapatillas"})), Completed("tool_calls")],
        [TextDelta("Encontré dos opciones."), Completed("stop")],
    ])
    world = ShoppingWorld(llm, tools=tools, timeout_seconds=5.0)

    events = await run_reference_turn(eval_case, world, "busco zapatillas")

    eval_case.check(
        "emitió el mensaje aceptado",
        isinstance(events[0], MessageAcceptedEvent),
        detail=[type(event).__name__ for event in events],
    )
    eval_case.check(
        "emitió el bloque de la herramienta",
        any(isinstance(event, BlockEvent) and event.block == block for event in events),
        detail=block_types(events),
    )
    eval_case.check(
        "cerró con el mensaje final",
        isinstance(events[-1], FinalEvent) and events[-1].message.text == "Encontré dos opciones.",
        detail=[type(event).__name__ for event in events],
    )
    eval_case.check("registró una llamada", len(world.tools.calls) == 1, detail=world.tools.names)
    if world.tools.calls:
        eval_case.check(
            "registró nombre y argumentos",
            (world.tools.calls[0].name, world.tools.calls[0].arguments) == ("buscar_productos", {"q": "zapatillas"}),
            detail={"name": world.tools.calls[0].name, "arguments": world.tools.calls[0].arguments},
        )
    eval_case.check("el reporte registró las aserciones", len(eval_case.assertions) >= 4, detail=len(eval_case.assertions))
