"""Internal e-mail for suggestions and bugs (SPEC-24), reusing SPEC-16's sender."""
from src.modules.mod_orders.ports.email_sender_port import EmailMessage, EmailSenderPort


class InternalEmailNotifier:
    def __init__(self, sender: EmailSenderPort, inbox: str):
        self._sender = sender
        self._inbox = inbox

    async def send(self, payload: dict) -> None:
        kind = str(payload.get("tipo") or "SUGGESTION")
        title = str(payload.get("titulo") or "")
        description = str(payload.get("descripcion") or "")
        contact = str(payload.get("correo") or "")
        screenshot = payload.get("captura")
        screenshot_url = payload.get("capturaUrl")
        lines = [
            f"Tipo: {kind}",
            f"Título: {title}",
            f"Descripción: {description}",
            f"Correo de contacto: {contact}",
        ]
        if screenshot_url:
            lines.append(f"Captura: {screenshot_url}")
        elif screenshot:
            lines.append(f"Captura (clave privada): {screenshot}")
        text = "\n".join(lines)
        await self._sender.send(
            EmailMessage(
                to=self._inbox,
                subject=f"[Botleta] {kind}: {title}"[:150],
                html=f"<pre>{text}</pre>",
                text=text,
            )
        )
