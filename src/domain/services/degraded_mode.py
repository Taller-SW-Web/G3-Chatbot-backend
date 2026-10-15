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
    r"paga\w*|cambia\w*|pon|cancela\w*|anula\w*|devuelve\w*|"
    r"descuento\w*|ignora\w*|instruccion\w*)\b",
)


class DegradedMode:
    @staticmethod
    def activo(*, llm_disponible: bool) -> bool:
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
        palabras_clave = tuple(dict.fromkeys(
            palabra for palabra in re.findall(r"\w+", normalizado) if palabra in _CATEGORIES
        ))
        if not _UNSAFE.search(normalizado) and palabras_clave:
            return DegradedResponse(
                aviso + "Voy a buscar productos con esas palabras.",
                _MENU, "buscar_productos", " ".join(palabras_clave),
            )
        return DegradedResponse(
            aviso + "Estoy teniendo problemas para entenderte, pero puedes usar estas opciones",
            _MENU,
        )
