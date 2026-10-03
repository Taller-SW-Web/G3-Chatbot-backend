"""Redacción de texto antes de persistir y enviar al LLM (SPEC-05 / ADR-0006)."""

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FilteredText:
    texto: str
    categorias: tuple[str, ...]
    avisos: tuple[str, ...]


_PASSWORD = re.compile(
    r"(?P<label>\b(?:contrase(?:ñ|n)a|password|clave)\s*(?:(?:es|:|=)\s*)?)"
    r"(?P<secreto>\[contraseña oculta\]|\"[^\"\n]+\"|'[^'\n]+'|[^\s,;]+)", re.IGNORECASE,
)
_DOCUMENT_LABEL = re.compile(
    r"\b(?P<tipo>DNI|RUC|CE|documento|carn[eé](?:\s+de\s+extranjer[ií]a)?|pasaporte)\b",
    re.IGNORECASE,
)
_DOCUMENT_NUMBER = re.compile(r"(?<!\w)(?=[A-Z0-9]*[0-9])[A-Z0-9]{6,20}(?!\w)", re.IGNORECASE)
_FORMATTED_DNI = re.compile(r"(?<!\w)(?:[0-9][ .-]?){7}[0-9](?![ .-]?[0-9])(?!\w)")
_FORMATTED_RUC = re.compile(r"(?<!\w)(?:[0-9][ .-]?){10}[0-9](?![ .-]?[0-9])(?!\w)")
_CARD = re.compile(r"(?<!\w)[0-9]+(?:[ -][0-9]+)*(?!\w)")
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
        """OTP depende del estado de la conversación, no del texto del usuario.

        Las imágenes y las direcciones libres no se procesan (ADR-0006).
        El resultado nunca contiene copias de las coincidencias sensibles.
        """
        detectadas: set[str] = set()

        def password(match: re.Match[str]) -> str:
            if match["secreto"] == "[contraseña oculta]":
                return match[0]
            if match["secreto"].casefold() in {
                "olvidada", "olvidé", "olvide", "de", "del", "para", "incorrecta",
                "recuperar", "cambiar", "restablecer",
                "ayer", "hoy", "antes",
            } and not re.search(r"[:=]|\bes\b", match["label"], re.IGNORECASE):
                return match[0]
            detectadas.add("CONTRASENA")
            return match["label"] + "[contraseña oculta]"

        def documentos(value: str) -> str:
            etiquetas = list(_DOCUMENT_LABEL.finditer(value))
            reemplazos: list[tuple[int, int]] = []
            for index, etiqueta in enumerate(etiquetas):
                limite = etiquetas[index + 1].start() if index + 1 < len(etiquetas) else len(value)
                tipo = etiqueta["tipo"].casefold()
                if tipo == "dni":
                    patrones = (_FORMATTED_DNI,)
                elif tipo == "ruc":
                    patrones = (_FORMATTED_RUC,)
                elif tipo == "documento":
                    patrones = (_FORMATTED_DNI, _FORMATTED_RUC, _DOCUMENT_NUMBER)
                else:
                    patrones = (_DOCUMENT_NUMBER,)
                candidatos = sorted(
                    (match for patron in patrones
                     for match in patron.finditer(value, etiqueta.end(), limite)),
                    key=lambda match: match.start(),
                )
                if candidatos:
                    reemplazos.append(candidatos[0].span())
                    detectadas.add("DOCUMENTO")
            for inicio, fin in reversed(reemplazos):
                value = value[:inicio] + "[documento oculto]" + value[fin:]
            return value

        def tarjeta(match: re.Match[str]) -> str:
            # Un número vecino ("tarjeta ... 2 unidades") no debe impedir
            # detectar el PAN. Solo cortar en separadores, nunca dentro de
            # una secuencia continua de más de 19 dígitos.
            partes = re.split(r"([ -])", match[0])
            salida: list[str] = []
            index = 0
            while index < len(partes):
                if index % 2:
                    salida.append(partes[index])
                    index += 1
                    continue
                numero = ""
                encontrado: int | None = None
                for final in range(index, len(partes), 2):
                    numero += partes[final]
                    if len(numero) > 19:
                        break
                    if _luhn(numero):
                        encontrado = final
                        break
                if encontrado is None:
                    salida.append(partes[index])
                    index += 1
                else:
                    detectadas.add("TARJETA")
                    salida.append("[tarjeta oculta]")
                    index = encontrado + 1
            return "".join(salida)

        def otp(match: re.Match[str]) -> str:
            detectadas.add("OTP")
            return "[OTP oculto]"

        texto = _PASSWORD.sub(password, texto)
        texto = documentos(texto)
        texto = _CARD.sub(tarjeta, texto)
        if otp_solicitado:
            texto = _OTP.sub(otp, texto)
        categorias = tuple(c for c in _WARNINGS if c in detectadas)
        return FilteredText(texto, categorias, tuple(_WARNINGS[c] for c in categorias))
