"""Public surface of `mod-shipping`.

Only the dispatch port and the quote helpers cross the boundary: `mod-orders`
reaches the port through the `dispatch_port` capability and `mod-checkout`
reuses the quote parsing helpers declared here.
"""
from src.modules.mod_shipping.application.shipping_quote import (
    build_destination,
    build_quote_lines,
    parse_quote,
)
from src.modules.mod_shipping.module import (
    DISPATCH_PORT,
    provide_despacho_client,
    provide_shipping_service,
)
from src.modules.mod_shipping.ports.dispatch_port import DispatchPort

__all__ = [
    "DISPATCH_PORT",
    "DispatchPort",
    "build_destination",
    "build_quote_lines",
    "parse_quote",
    "provide_despacho_client",
    "provide_shipping_service",
]
