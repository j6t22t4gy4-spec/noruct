"""Company context hook for the Noruct Hermes application fork."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


def _freeze(value: Any) -> Any:
    """Recursively freeze the parent projection before it enters the agent."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def attach_company_context(agent: Any, context: Mapping[str, Any] | None = None) -> None:
    """Attach an immutable Company task projection to a Hermes agent.

    The projection is deliberately data-only.  It does not install handlers,
    choose a provider, mutate Hermes session state, or grant an effect.  The
    next fork step will consume this projection at the product prompt/event
    seam while Noruct's parent remains the authority for all side effects.
    """

    values = {
        str(key): _freeze(value)
        for key, value in (context or {}).items()
        if isinstance(key, str)
    }
    setattr(agent, "noruct_company_context", MappingProxyType(values))


def company_context(agent: Any) -> Mapping[str, Any]:
    """Return the immutable Company projection attached to ``agent``."""

    value = getattr(agent, "noruct_company_context", None)
    return value if isinstance(value, Mapping) else MappingProxyType({})
