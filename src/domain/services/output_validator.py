"""Verifica menciones monetarias del texto final contra datos de herramientas."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal


_NUMBER = r"(?<![\d.,])[-+]?[0-9]+(?:[., ][0-9]+)*(?!\d|[.,]\d)"
_AMOUNT = re.compile(
    rf"(?:(?:S/\.?|PEN)\s*(?P<antes>{_NUMBER})"
    rf"|(?P<despues>{_NUMBER})\s*(?:soles|PEN)\b)", re.IGNORECASE,
)
_PERCENT = re.compile(rf"(?P<numero>{_NUMBER})\s*%")
_DISCOUNT_AFTER = re.compile(
    r"^\s*(?:(?:de|del|en)\s+)?(?:descuentos?|ahorro|rebajas?|off)\b", re.IGNORECASE,
)
_DISCOUNT_BEFORE = re.compile(
    r"\b(?:descuentos?|ahorro|rebajas?|ofertas?|promoci[oó]n)"
    r"(?:\s+(?:de|del|es))?\s*[:=]?\s*$", re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ValidatedOutput:
    texto: str
    corregido: bool


def _decimal(texto: str) -> Decimal | None:
    """Parsea formatos PEN comunes y rechaza separadores malformados."""
    signo = ""
    if texto[:1] in {"+", "-"}:
        signo, texto = texto[0], texto[1:]

    decimal_sep: str | None = None
    separadores_decimales = [i for i, char in enumerate(texto) if char in ".,"]
    if separadores_decimales:
        ultimo = separadores_decimales[-1]
        cantidad_final = len(texto) - ultimo - 1
        if 1 <= cantidad_final <= 2:
            decimal_sep = texto[ultimo]
            parte_entera, parte_decimal = texto[:ultimo], texto[ultimo + 1:]
            if decimal_sep in parte_entera:
                return None
        else:
            parte_entera, parte_decimal = texto, ""
    else:
        parte_entera, parte_decimal = texto, ""

    separadores_grupo = {char for char in parte_entera if char in "., "}
    if separadores_grupo:
        if len(separadores_grupo) != 1:
            return None
        separador = next(iter(separadores_grupo))
        grupos = parte_entera.split(separador)
        if not (1 <= len(grupos[0]) <= 3 and grupos[0].isdigit()):
            return None
        if any(len(grupo) != 3 or not grupo.isdigit() for grupo in grupos[1:]):
            return None
        digitos = "".join(grupos)
    else:
        if not parte_entera.isdigit():
            return None
        digitos = parte_entera

    if parte_decimal and not (parte_decimal.isdigit() and len(parte_decimal) <= 2):
        return None
    normalizado = signo + digitos
    if decimal_sep is not None:
        normalizado += "." + parte_decimal
    return Decimal(normalizado)


class OutputValidator:
    def validar(
        self, texto: str, *, montos: Iterable[Decimal],
        porcentajes: Iterable[Decimal] = (),
    ) -> ValidatedOutput:
        """El caso de uso extrae valores autorizados de resultados de tools.

        No aceptar listas generadas por el LLM. La salida sustituye menciones
        no respaldadas; el orquestador la publica en fin. Los bloques conservan
        los datos comerciales verídicos. No prueba la asociación producto/precio.
        """
        montos_validos, porcentajes_validos = set(montos), set(porcentajes)
        corregido = False

        def importe(match: re.Match[str]) -> str:
            nonlocal corregido
            valor = _decimal(match["antes"] or match["despues"])
            if valor in montos_validos:
                return match[0]
            corregido = True
            return "[importe no verificado]"

        def porcentaje(match: re.Match[str]) -> str:
            nonlocal corregido
            if not (
                _DISCOUNT_AFTER.match(match.string[match.end():])
                or _DISCOUNT_BEFORE.search(match.string[:match.start()])
            ):
                # Composición textil y otras características se verifican en
                # los bloques/prompt; no son porcentajes de descuento.
                return match[0]
            if _decimal(match["numero"]) in porcentajes_validos:
                return match[0]
            corregido = True
            return "[descuento no verificado]"

        texto = _AMOUNT.sub(importe, texto)
        texto = _PERCENT.sub(porcentaje, texto)
        return ValidatedOutput(texto, corregido)
