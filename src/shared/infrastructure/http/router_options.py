"""Options the composition root passes to every module router factory.

The bag is generic: modules read only the fields they need and ignore the rest.
Tests use `turn_scope`/`action_scope` to replace the chat scopes; production
leaves them unset or resolves the `action_scope` capability in the root.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RouterOptions:
    turn_scope: Any = None
    action_scope: Any = None


DEFAULT_ROUTER_OPTIONS = RouterOptions()
