"""Redaction of user text before persistence and LLM prompting (SPEC-05 / ADR-0006)."""

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FilteredText:
    texto: str
    categorias: tuple[str, ...]
    avisos: tuple[str, ...]


_PASSWORD_ASSIGNMENT = re.compile(
    r"(?P<label>(?<!\w)(?:contrase(?:ñ|n)a|password|(?<!palabra )clave)\b"
    r"(?:(?![.!?;\n]).){0,100}?(?:\bes\b|[:=])\s*)",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|[;\n]")
_PASSWORD_PLACEHOLDER = "[contraseña oculta]"

_DOCUMENT_LABEL = re.compile(
    r"\b(?P<tipo>carn[eé](?:\s+de\s+extranjer[ií]a)?|DNI|RUC|CE|documento|pasaporte)\b",
    re.IGNORECASE,
)
_DOCUMENT_BRIDGE = re.compile(
    r"\s*(?:(?:de\s+identidad)\s*)?(?:(?:es(?:\s+el)?|n(?:ú|u)mero|[:#])\s*)?",
    re.IGNORECASE,
)
_DNI_VALUE = re.compile(r"(?:\d{8}|\d{2}[ .-]\d{3}[ .-]\d{3})(?!\w|\.\d)")
_RUC_VALUE = re.compile(r"(?:\d{11}|\d{2}[ .-]\d{3}[ .-]\d{5}[ .-]\d)(?!\w|\.\d)")
_DOCUMENT_VALUE = re.compile(
    r"(?:\d{8}|\d{2}[ .-]\d{3}[ .-]\d{3}|\d{11}|\d{2}[ .-]\d{3}[ .-]\d{5}[ .-]\d)(?!\w|\.\d)"
)
_CE_VALUE = re.compile(r"\d{9}(?!\w|\.\d)")
_PASSPORT_VALUE = re.compile(r"(?=[A-Z0-9]*\d)[A-Z0-9]{6,12}(?!\w)", re.IGNORECASE)
_DOCUMENT_VALUES = {
    "dni": _DNI_VALUE,
    "ruc": _RUC_VALUE,
    "ce": _CE_VALUE,
    "documento": _DOCUMENT_VALUE,
    "carné": _CE_VALUE,
    "carné de extranjería": _CE_VALUE,
    "pasaporte": _PASSPORT_VALUE,
}

_CARD_RUN = re.compile(r"(?<!\d)\d(?:[\d .-]*\d)?(?!\d)")
_OTP = re.compile(r"(?<!\w)[0-9]{6}(?!\w)")
_WARNINGS = {
    "TARJETA": "Por tu seguridad, ingresa los datos de tu tarjeta solo en la pantalla de pago",
    "DOCUMENTO": "Por tu seguridad, el documento se ingresa en la pantalla de pago",
    "OTP": "Por tu seguridad, ingresa el código solo en el formulario de verificación",
    "CONTRASENA": "Por tu seguridad, ingresa tu contraseña solo en el formulario correspondiente",
}


def _luhn(numero: str) -> bool:
    digitos = [int(d) for d in numero if d.isdigit()]
    if not 13 <= len(digitos) <= 19:
        return False
    total = 0
    for index, digito in enumerate(reversed(digitos)):
        if index % 2:
            digito *= 2
            if digito > 9:
                digito -= 9
        total += digito
    return total % 10 == 0


class SensitiveDataFilter:
    def redactar(self, texto: str, *, otp_solicitado: bool = False) -> FilteredText:
        """Redact labeled credentials, payment cards, identity numbers, and requested OTPs.

        OTP redaction depends on conversation state rather than message text. Images and free-form
        addresses are not processed (ADR-0006). The return value contains no matched secret.
        """
        detectadas: set[str] = set()
        texto = self._redactar_tarjetas(texto, detectadas)
        texto = self._redactar_documentos(texto, detectadas)
        texto = self._redactar_contrasenas(texto, detectadas)
        if otp_solicitado:
            texto = _OTP.sub(self._redactar_otp(detectadas), texto)
        categorias = tuple(c for c in _WARNINGS if c in detectadas)
        return FilteredText(texto, categorias, tuple(_WARNINGS[c] for c in categorias))

    @staticmethod
    def _redactar_tarjetas(texto: str, detectadas: set[str]) -> str:
        salida: list[str] = []
        cursor = 0
        while match := _CARD_RUN.search(texto, cursor):
            posiciones = [index for index, char in enumerate(match[0]) if char.isdigit()]
            tarjeta: tuple[int, int] | None = None
            for inicio_digitos, inicio in enumerate(posiciones):
                if inicio > 0 and match[0][inicio - 1].isdigit():
                    continue
                cantidad_maxima = min(len(posiciones) - inicio_digitos, 19)
                for cantidad in range(cantidad_maxima, 12, -1):
                    fin = posiciones[inicio_digitos + cantidad - 1] + 1
                    if fin < len(match[0]) and match[0][fin].isdigit():
                        continue
                    if _luhn(match[0][inicio:fin]):
                        tarjeta = (inicio, fin)
                        break
                if tarjeta is not None:
                    break

            if tarjeta is None:
                salida.append(texto[cursor:match.end()])
                cursor = match.end()
                continue

            inicio, fin = tarjeta
            inicio_absoluto = match.start() + inicio
            fin_absoluto = match.start() + fin
            salida.append(texto[cursor:inicio_absoluto])
            salida.append("[tarjeta oculta]")
            detectadas.add("TARJETA")
            cursor = fin_absoluto
        salida.append(texto[cursor:])
        return "".join(salida)

    @staticmethod
    def _redactar_documentos(texto: str, detectadas: set[str]) -> str:
        reemplazos: list[tuple[int, int]] = []
        for etiqueta in _DOCUMENT_LABEL.finditer(texto):
            tipo = etiqueta["tipo"].casefold()
            patron = _DOCUMENT_VALUES.get(tipo)
            if patron is None:
                continue
            cola = texto[etiqueta.end():]
            puente = _DOCUMENT_BRIDGE.match(cola)
            assert puente is not None
            numero = patron.match(cola, puente.end())
            if numero is not None:
                inicio = etiqueta.end() + numero.start()
                fin = etiqueta.end() + numero.end()
                reemplazos.append((inicio, fin))
                detectadas.add("DOCUMENTO")

        for inicio, fin in reversed(reemplazos):
            texto = texto[:inicio] + "[documento oculto]" + texto[fin:]
        return texto

    @staticmethod
    def _redactar_contrasenas(texto: str, detectadas: set[str]) -> str:
        reemplazos: list[tuple[int, int]] = []
        for asignacion in _PASSWORD_ASSIGNMENT.finditer(texto):
            fin = _SENTENCE_END.search(texto, asignacion.end())
            fin_secreto = fin.start() if fin is not None else len(texto)
            secreto = texto[asignacion.end():fin_secreto]
            if not secreto.strip() or secreto.lstrip().casefold().startswith(_PASSWORD_PLACEHOLDER):
                continue
            reemplazos.append((asignacion.end(), fin_secreto))
            detectadas.add("CONTRASENA")

        for inicio, fin in reversed(reemplazos):
            texto = texto[:inicio] + _PASSWORD_PLACEHOLDER + texto[fin:]
        return texto

    @staticmethod
    def _redactar_otp(detectadas: set[str]):
        def reemplazar(_: re.Match[str]) -> str:
            detectadas.add("OTP")
            return "[OTP oculto]"

        return reemplazar
