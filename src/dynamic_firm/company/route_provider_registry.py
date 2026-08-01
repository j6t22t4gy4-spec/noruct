"""Non-authoritative construction of an exact adapter for a frozen route."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .execution_route_binding import ExecutionRouteBinding
from .multi_route_runtime_policy import MultiRouteRuntimePolicy


class RouteAdapterFactory(Protocol):
    def __call__(self, binding: ExecutionRouteBinding) -> object: ...


@dataclass(frozen=True, slots=True)
class RouteProviderDefinition:
    route_id: str
    provider_config_digest: str
    credential_reference: str
    factory: RouteAdapterFactory

    def __post_init__(self) -> None:
        if not self.route_id or not self.provider_config_digest or not self.credential_reference or not callable(self.factory):
            raise ValueError("route provider definition is incomplete")


class FrozenRouteProviderRegistry:
    """Constructs; it never selects routes, reads a credential, or stores state."""

    def __init__(self, definitions: tuple[RouteProviderDefinition, ...]) -> None:
        if not definitions or len({item.route_id for item in definitions}) != len(definitions):
            raise ValueError("route provider definitions must be nonempty and unique")
        self._definitions = {item.route_id: item for item in definitions}

    def validate_frozen_bindings(
        self,
        policy: MultiRouteRuntimePolicy,
    ) -> tuple[tuple[str, str, str], ...]:
        """Prove the exact frozen-route adapter closure without construction.

        This is deliberately a metadata-only preflight.  It reads neither a
        credential nor an adapter factory, and it returns no adapter.  The
        immutable policy has already proven that every binding is assigned to
        the frozen Job; this registry proves that each such binding has one
        matching route definition at the exact provider configuration and
        credential-reference boundary.
        """
        if not isinstance(policy, MultiRouteRuntimePolicy):
            raise TypeError("a MultiRouteRuntimePolicy is required")
        metadata: list[tuple[str, str, str]] = []
        for binding in policy.bindings:
            definition = self._definitions.get(binding.route_id)
            if definition is None:
                raise ValueError("frozen route has no registered adapter")
            if definition.provider_config_digest != binding.provider_config_digest:
                raise ValueError("frozen route provider configuration drifted")
            if definition.credential_reference != binding.credential_reference:
                raise ValueError("frozen route credential reference drifted")
            metadata.append(
                (
                    binding.route_id,
                    binding.provider_config_digest,
                    binding.credential_reference,
                )
            )
        return tuple(sorted(metadata))

    def construct(self, binding: ExecutionRouteBinding) -> object:
        if not isinstance(binding, ExecutionRouteBinding):
            raise TypeError("an ExecutionRouteBinding is required")
        definition = self._definitions.get(binding.route_id)
        if definition is None:
            raise ValueError("frozen route has no registered adapter")
        if definition.provider_config_digest != binding.provider_config_digest:
            raise ValueError("frozen route provider configuration drifted")
        if definition.credential_reference != binding.credential_reference:
            raise ValueError("frozen route credential reference drifted")
        adapter = definition.factory(binding)
        # The mutable compatibility wrapper remains available only to the
        # legacy/default path.  A frozen route has an immutable fallback
        # policy digest and must use the admitted boundary instead; otherwise
        # a retry could select a child route without that Job's admission.
        from dynamic_firm.providers.fallback import FallbackModelProvider

        if isinstance(adapter, FallbackModelProvider):
            raise ValueError(
                "Frozen route adapters must not use legacy mutable fallback"
            )
        return adapter
