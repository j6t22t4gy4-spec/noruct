"""Immutable, source-bound projection of the seven organization routes.

This module owns no execution or admission authority. A plan contains only
references to the authorities that define each route; callers must provide
the currently observed authority bindings before the projection is usable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable, Mapping


ORGANIZATION_PLAN_SCHEMA = "noruct.frozen-organization-plan.v1"


class OrganizationPlanRoute(StrEnum):
    """The fixed route projections in a Frozen Organization Plan."""

    TASK_DEPENDENCY = "task/dependency"
    ASSIGNMENT = "assignment"
    INFORMATION_EVIDENCE = "information/evidence"
    ARTIFACT_COMMUNICATION = "artifact/communication"
    DECISION_EFFECT = "decision/effect"
    VERIFICATION = "verification"
    LEARNING_CANDIDATE = "learning-candidate"


_ROUTE_FIELDS = (
    "task_dependency",
    "assignment",
    "information_evidence",
    "artifact_communication",
    "decision_effect",
    "verification",
    "learning_candidate",
)
_ROUTE_BY_FIELD = dict(zip(_ROUTE_FIELDS, OrganizationPlanRoute))


class OrganizationPlanBindingError(ValueError):
    """Raised when a plan is missing an exact current source binding."""


@dataclass(frozen=True, slots=True)
class SourceAuthorityBinding:
    """The opaque identity and content digest of one source authority."""

    authority_id: str
    authority_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority_id, str) or not self.authority_id:
            raise ValueError("authority_id must be a non-empty string")
        if not isinstance(self.authority_digest, str) or not self.authority_digest:
            raise ValueError("authority_digest must be a non-empty string")


@dataclass(frozen=True, slots=True)
class FrozenOrganizationPlan:
    """Read-only seven-route projection with no dispatch capability."""

    task_dependency: SourceAuthorityBinding
    assignment: SourceAuthorityBinding
    information_evidence: SourceAuthorityBinding
    artifact_communication: SourceAuthorityBinding
    decision_effect: SourceAuthorityBinding
    verification: SourceAuthorityBinding
    learning_candidate: SourceAuthorityBinding
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        bindings = self._bindings()
        for route, binding in bindings:
            if not isinstance(binding, SourceAuthorityBinding):
                raise TypeError(f"{route.value} must be a SourceAuthorityBinding")

        authority_digests: dict[str, str] = {}
        for _, binding in bindings:
            previous_digest = authority_digests.setdefault(
                binding.authority_id,
                binding.authority_digest,
            )
            if previous_digest != binding.authority_digest:
                raise ValueError(
                    "Conflicting digests for source authority "
                    f"{binding.authority_id!r}"
                )
        object.__setattr__(self, "digest", hashlib.sha256(self.canonical_bytes()).hexdigest())

    def _bindings(self) -> tuple[tuple[OrganizationPlanRoute, SourceAuthorityBinding], ...]:
        return tuple(
            (route, getattr(self, field_name))
            for field_name, route in _ROUTE_BY_FIELD.items()
        )

    @property
    def routes(self) -> tuple[tuple[OrganizationPlanRoute, SourceAuthorityBinding], ...]:
        """Return exactly the seven route/source-reference pairs."""

        return self._bindings()

    @property
    def source_bindings(self) -> tuple[SourceAuthorityBinding, ...]:
        """Return the source references in fixed route order."""

        return tuple(binding for _, binding in self._bindings())

    def canonical_payload(self) -> dict[str, object]:
        """Return the fixed-field payload used for the plan digest."""

        return {
            "schema": ORGANIZATION_PLAN_SCHEMA,
            "routes": [
                {
                    "route": route.value,
                    "authority_id": binding.authority_id,
                    "authority_digest": binding.authority_digest,
                }
                for route, binding in self._bindings()
            ],
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")

    @property
    def content_digest(self) -> str:
        """Digest alias for immutable projection consumers."""

        return self.digest

    def validate(
        self,
        observed_bindings: Mapping[str, str] | Iterable[SourceAuthorityBinding],
    ) -> bool:
        """Validate every route against exact currently observed bindings.

        The observed input may contain unrelated authorities, but every
        authority referenced by this plan must be present with the same digest.
        Missing and stale references raise the same fail-closed error type.
        """

        observed = _normalize_observed_bindings(observed_bindings)
        for route, binding in self._bindings():
            observed_digest = observed.get(binding.authority_id)
            if observed_digest is None:
                raise OrganizationPlanBindingError(
                    f"Missing source authority binding for {route.value}: "
                    f"{binding.authority_id}"
                )
            if observed_digest != binding.authority_digest:
                raise OrganizationPlanBindingError(
                    f"Stale source authority binding for {route.value}: "
                    f"{binding.authority_id}"
                )
        return True

    def validate_source_bindings(
        self,
        observed_bindings: Mapping[str, str] | Iterable[SourceAuthorityBinding],
    ) -> bool:
        """Explicit alias for validating the plan's source bindings."""

        return self.validate(observed_bindings)

    @classmethod
    def from_routes(
        cls,
        routes: Mapping[OrganizationPlanRoute | str, SourceAuthorityBinding]
        | Iterable[tuple[OrganizationPlanRoute | str, SourceAuthorityBinding]],
    ) -> "FrozenOrganizationPlan":
        """Build a plan while requiring the fixed set of seven route names."""

        if isinstance(routes, Mapping):
            items = tuple(routes.items())
        else:
            items = tuple(routes)

        normalized: dict[OrganizationPlanRoute, SourceAuthorityBinding] = {}
        for route, binding in items:
            try:
                route_name = (
                    route
                    if isinstance(route, OrganizationPlanRoute)
                    else OrganizationPlanRoute(route)
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Unknown organization plan route: {route!r}") from exc
            if route_name in normalized:
                raise ValueError(f"Duplicate organization plan route: {route_name.value}")
            normalized[route_name] = binding

        expected = set(OrganizationPlanRoute)
        if set(normalized) != expected:
            missing = sorted(route.value for route in expected - set(normalized))
            extra = sorted(route.value for route in set(normalized) - expected)
            details = []
            if missing:
                details.append(f"missing={missing}")
            if extra:
                details.append(f"extra={extra}")
            raise ValueError(
                "Organization plan must contain exactly seven routes ("
                + ", ".join(details)
                + ")"
            )

        return cls(
            **{
                field_name: normalized[route]
                for field_name, route in _ROUTE_BY_FIELD.items()
            }
        )


def _normalize_observed_bindings(
    observed_bindings: Mapping[str, str] | Iterable[SourceAuthorityBinding],
) -> dict[str, str]:
    if isinstance(observed_bindings, Mapping):
        normalized: dict[str, str] = {}
        for authority_id, authority_digest in observed_bindings.items():
            if not isinstance(authority_id, str) or not authority_id:
                raise OrganizationPlanBindingError("Observed authority id must be non-empty")
            if not isinstance(authority_digest, str) or not authority_digest:
                raise OrganizationPlanBindingError(
                    f"Observed authority digest must be non-empty for {authority_id}"
                )
            normalized[authority_id] = authority_digest
        return normalized

    try:
        observed = tuple(observed_bindings)
    except TypeError as exc:
        raise TypeError("observed_bindings must be a mapping or binding iterable") from exc

    normalized = {}
    for binding in observed:
        if not isinstance(binding, SourceAuthorityBinding):
            raise TypeError("Observed binding iterable must contain SourceAuthorityBinding values")
        previous_digest = normalized.setdefault(binding.authority_id, binding.authority_digest)
        if previous_digest != binding.authority_digest:
            raise OrganizationPlanBindingError(
                f"Conflicting observed digests for source authority {binding.authority_id!r}"
            )
    return normalized


__all__ = [
    "FrozenOrganizationPlan",
    "ORGANIZATION_PLAN_SCHEMA",
    "OrganizationPlanBindingError",
    "OrganizationPlanRoute",
    "SourceAuthorityBinding",
]
