"""Options the composition root passes to every module router factory.

Only `mod-chat` reads them today: tests replace the turn/action scopes behind
the streaming and direct-action endpoints. Modules that do not need them
accept the object and ignore it.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RouterOptions:
    turn_scope: Any = None
    action_scope: Any = None


DEFAULT_ROUTER_OPTIONS = RouterOptions()
