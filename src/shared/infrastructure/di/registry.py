"""Cross-module capability registry.

The manifest's DAG forbids some direct imports (for example `mod-checkout`
needs the order-confirmation service owned by `mod-orders`, and `mod-orders`
needs the dispatch port owned by `mod-shipping`). Instead of importing the
other module's interior, the owning module registers a named provider in
`configure()` and the consumer resolves it at request time.

It is a registry of *capabilities*, not a module list: nothing here knows how
many modules exist.
"""
from collections.abc import Callable
from typing import Any


class CapabilityNotRegistered(LookupError):
    """A module asked for a capability that no module registered."""


Provider = Callable[..., Any]


class CapabilityRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, name: str, provider: Provider) -> None:
        self._providers[name] = provider

    def provider(self, name: str) -> Provider:
        try:
            return self._providers[name]
        except KeyError:
            raise CapabilityNotRegistered(
                f"capability {name!r} is not registered; is its owning module discovered?"
            ) from None

    def is_registered(self, name: str) -> bool:
        return name in self._providers

    def resolve(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return self.provider(name)(*args, **kwargs)

    def clear(self) -> None:
        """Test helper: forget every registered capability."""
        self._providers.clear()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


registry = CapabilityRegistry()
