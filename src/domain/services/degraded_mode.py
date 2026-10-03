"""Menú e intérprete seguro sin LLM (SPEC-05, requisito 10)."""

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QuickAction:
    etiqueta: str
    tipo: str


@dataclass(frozen=True, slots=True)
class DegradedResponse:
    texto: str
    acciones: tuple[QuickAction, ...]
    herramienta: str | None = None
    consulta: str | None = None


_MENU = (
    QuickAction("Buscar por categoría", "BUSCAR_PRODUCTOS"),
    QuickAction("Ofertas", "CONSULTAR_PROMOCIONES"),
    QuickAction("Carrito", "VER_CARRITO"),
    QuickAction("Mis pedidos", "LISTAR_PEDIDOS"),
    QuickAction("Mis reclamos", "LISTAR_RECLAMOS"),
    QuickAction("Mis devoluciones", "LISTAR_DEVOLUCIONES"),
)
_CATEGORIES = frozenset({
    "zapatillas", "chimpunes", "tachos", "polos", "polo", "camisetas",
    "pelotas", "buzos", "buzo", "medias", "shorts", "calzado",
    "zapatilla", "chimpun", "tacho", "camiseta", "pelota", "media", "short",
})
_UNSAFE = re.compile(
    r"\b(?:agrega\w*|anad\w*|quita\w*|elimina\w*|vacia\w*|borra\w*|"
    r"compra\w*|paga\w*|cambia\w*|pon|descuento\w*|ignora\w*|instruccion\w*)\b",
)


class DegradedMode:
    @staticmethod
    def activo(*, llm_disponible: bool, websocket_disponible: bool = True) -> bool:
        # Si solo falla WS, todavía puede obtenerse respuesta LLM por REST/polling.
        return not llm_disponible

    def responder(self, texto: str, *, fallo_imagen: bool = False) -> DegradedResponse:
        """Recibe texto previamente redactado. No ejecuta herramientas ni I/O.

        El orquestador ejecuta el plan por ToolRegistry y responde por REST.
        El menú incluye capacidades H4 para las que el equipo debe registrar
        handlers cuando las implemente; nunca simula sus resultados.
        """
        normalizado = "".join(
            c for c in unicodedata.normalize("NFD", texto.casefold())
            if unicodedata.category(c) != "Mn"
        )
        aviso = "No pude analizar la imagen. " if fallo_imagen else ""
        if not _UNSAFE.search(normalizado) and _CATEGORIES.intersection(re.findall(r"\w+", normalizado)):
            return DegradedResponse(
                aviso + "Voy a buscar productos con esas palabras.",
                _MENU, "buscar_productos", texto.strip()[:1000],
            )
        return DegradedResponse(
            aviso + "Estoy teniendo problemas para entenderte, pero puedes usar estas opciones",
            _MENU,
        )
