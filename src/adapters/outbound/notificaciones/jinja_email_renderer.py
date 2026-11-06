"""Jinja2 renderer for the order confirmation e-mail (SPEC-16).

Templates live next to this module (`templates/confirmacion_pedido.html`
and `.txt`). The HTML version is autoescaped; the text version is not.
"""
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from src.ports.outbound.email_sender_port import RenderedEmail

TEMPLATES_DIR = Path(__file__).parent / "templates"


class JinjaEmailRenderer:
    def __init__(self, templates_dir: Path = TEMPLATES_DIR):
        self._env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=False),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render_order_confirmation(self, context: dict[str, Any]) -> RenderedEmail:
        return RenderedEmail(
            subject=context["asunto"],
            html=self._env.get_template("confirmacion_pedido.html").render(**context),
            text=self._env.get_template("confirmacion_pedido.txt").render(**context),
        )
