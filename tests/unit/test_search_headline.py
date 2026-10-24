"""H3-15: parsing of ts_headline output into highlight segments (SPEC-05 Req. 1 · Buscar chats)."""
from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    HIGHLIGHT_START as S,
)
from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    HIGHLIGHT_STOP as E,
)
from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    parse_headline,
)
from src.domain.entities.conversation import HighlightSegment as Seg


def test_separa_texto_plano_y_coincidencias():
    assert parse_headline(f"busco {S}zapatillas{E} para {S}correr{E} hoy") == (
        Seg("busco ", False), Seg("zapatillas", True), Seg(" para ", False),
        Seg("correr", True), Seg(" hoy", False),
    )


def test_coincidencia_al_inicio_o_al_final_no_deja_segmentos_vacios():
    assert parse_headline(f"{S}Zapatillas{E}") == (Seg("Zapatillas", True),)
    assert parse_headline(f"{S}Zapatillas{E} rojas") == (Seg("Zapatillas", True), Seg(" rojas", False))


def test_sin_marcas_es_un_solo_segmento_plano():
    assert parse_headline("chat sin coincidencia") == (Seg("chat sin coincidencia", False),)
    assert parse_headline("") == ()


def test_el_html_del_usuario_queda_como_texto():
    assert parse_headline(f"<b>{S}hola{E}</b>") == (Seg("<b>", False), Seg("hola", True), Seg("</b>", False))
