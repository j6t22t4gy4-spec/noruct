"""Provider-free admission for reusing an already frozen multi-route plan.

This is deliberately a local policy check, not route selection.  It compares
the bindings already fixed in a ``MultiRouteRuntimePolicy`` with the user's
non-secret approved-route registry before any provider registry can be used.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from .execution_route_binding import ExecutionRouteBinding
from .multi_route_runtime_policy import MultiRouteRuntimePolicy
from .user_routing_policy import ApprovedRouteRegistry, UserRoutingPolicy


class ApprovedRoutePlanAdmissionDisposition(StrEnum):
    ADMITTED_APPROVED_REUSE = "ADMITTED_APPROVED_REUSE"
    DENIED_FIRST_RUN_NO_APPROVED_ROUTES = "DENIED_FIRST_RUN_NO_APPROVED_ROUTES"
    DENIED_MISSING_ROUTE_APPROVAL = "DENIED_MISSING_ROUTE_APPROVAL"
    DENIED_EXECUTION_ROUTE_BINDING_DIGEST_MISMATCH = "DENIED_EXECUTION_ROUTE_BINDING_DIGEST_MISMATCH"
    DENIED_PROVIDER_CONFIG_DIGEST_MISMATCH = "DENIED_PROVIDER_CONFIG_DIGEST_MISMATCH"
    DENIED_CREDENTIAL_REFERENCE_MISMATCH = "DENIED_CREDENTIAL_REFERENCE_MISMATCH"
    DENIED_DUPLICATE_OR_INCOMPLETE_BINDING_COVERAGE = "DENIED_DUPLICATE_OR_INCOMPLETE_BINDING_COVERAGE"


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return value


@dataclass(frozen=True, slots=True)
class ApprovedRoutePlanAdmission:
    """Content-free local receipt; it cannot carry route execution authority."""

    disposition: ApprovedRoutePlanAdmissionDisposition
    policy_digest: str
    registry_digest: str
    runtime_policy_summary_digest: str
    approved_binding_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, ApprovedRoutePlanAdmissionDisposition):
            object.__setattr__(self, "disposition", ApprovedRoutePlanAdmissionDisposition(self.disposition))
        for field in ("policy_digest", "registry_digest", "runtime_policy_summary_digest"):
            object.__setattr__(self, field, _digest(getattr(self, field), field))
        if not isinstance(self.approved_binding_count, int) or isinstance(self.approved_binding_count, bool) or self.approved_binding_count < 0:
            raise ValueError("approved_binding_count must be a non-negative integer")
        admitted = self.disposition is ApprovedRoutePlanAdmissionDisposition.ADMITTED_APPROVED_REUSE
        if admitted != (self.approved_binding_count > 0):
            raise ValueError("only an admitted decision may report approved bindings")

    @property
    def admitted(self) -> bool:
        return self.disposition is ApprovedRoutePlanAdmissionDisposition.ADMITTED_APPROVED_REUSE

    def canonical_payload(self) -> dict[str, str | int]:
        """Content-free receipt fields only; this is not a route serialization."""
        return {
            "approved_binding_count": self.approved_binding_count,
            "disposition": self.disposition.value,
            "policy_digest": self.policy_digest,
            "registry_digest": self.registry_digest,
            "runtime_policy_summary_digest": self.runtime_policy_summary_digest,
        }

    def canonical_summary(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    @property
    def summary_digest(self) -> str:
        return hashlib.sha256(self.canonical_summary().encode("utf-8")).hexdigest()


def _has_complete_unique_binding_coverage(runtime_policy: MultiRouteRuntimePolicy) -> bool:
    bindings = runtime_policy.bindings
    if not isinstance(bindings, tuple) or not all(isinstance(binding, ExecutionRouteBinding) for binding in bindings):
        return False
    expected = tuple(assignment.route_binding_digest for assignment in runtime_policy.plan.assignments)
    actual = tuple(binding.digest for binding in bindings)
    return (
        len(expected) == len(actual)
        and len(set(expected)) == len(expected)
        and len(set(actual)) == len(actual)
        and set(expected) == set(actual)
        and len({binding.route_id for binding in bindings}) == len(bindings)
    )


def admit_approved_route_plan(
    policy: UserRoutingPolicy,
    registry: ApprovedRouteRegistry,
    runtime_policy: MultiRouteRuntimePolicy,
) -> ApprovedRoutePlanAdmission:
    """Admit an exact frozen plan only when every binding has local approval.

    No policy mode participates in ranking or route choice.  The mode's digest
    is evidence of the user's local policy state only.
    """
    if not isinstance(policy, UserRoutingPolicy):
        raise TypeError("a typed user routing policy is required")
    if not isinstance(registry, ApprovedRouteRegistry):
        raise TypeError("a typed approved route registry is required")
    if not isinstance(runtime_policy, MultiRouteRuntimePolicy):
        raise TypeError("a typed frozen multi-route runtime policy is required")

    base = {
        "policy_digest": policy.digest,
        "registry_digest": registry.digest,
        "runtime_policy_summary_digest": runtime_policy.summary_digest,
    }
    if not _has_complete_unique_binding_coverage(runtime_policy):
        return ApprovedRoutePlanAdmission(
            disposition=ApprovedRoutePlanAdmissionDisposition.DENIED_DUPLICATE_OR_INCOMPLETE_BINDING_COVERAGE,
            approved_binding_count=0,
            **base,
        )
    if not registry.routes:
        return ApprovedRoutePlanAdmission(
            disposition=ApprovedRoutePlanAdmissionDisposition.DENIED_FIRST_RUN_NO_APPROVED_ROUTES,
            approved_binding_count=0,
            **base,
        )

    approved_by_route = {metadata.route_id: metadata for metadata in registry.routes}
    for binding in runtime_policy.bindings:
        metadata = approved_by_route.get(binding.route_id)
        if metadata is None:
            disposition = ApprovedRoutePlanAdmissionDisposition.DENIED_MISSING_ROUTE_APPROVAL
        elif metadata.execution_route_binding_digest != binding.digest:
            disposition = ApprovedRoutePlanAdmissionDisposition.DENIED_EXECUTION_ROUTE_BINDING_DIGEST_MISMATCH
        elif metadata.provider_config_digest != binding.provider_config_digest:
            disposition = ApprovedRoutePlanAdmissionDisposition.DENIED_PROVIDER_CONFIG_DIGEST_MISMATCH
        elif metadata.credential_reference != binding.credential_reference:
            disposition = ApprovedRoutePlanAdmissionDisposition.DENIED_CREDENTIAL_REFERENCE_MISMATCH
        else:
            continue
        return ApprovedRoutePlanAdmission(
            disposition=disposition,
            approved_binding_count=0,
            **base,
        )
    return ApprovedRoutePlanAdmission(
        disposition=ApprovedRoutePlanAdmissionDisposition.ADMITTED_APPROVED_REUSE,
        approved_binding_count=len(runtime_policy.bindings),
        **base,
    )


def require_fresh_approved_route_plan(
    policy: UserRoutingPolicy,
    registry: ApprovedRouteRegistry,
    runtime_policy: MultiRouteRuntimePolicy,
) -> MultiRouteRuntimePolicy:
    """Re-admit fresh typed inputs and return the supplied policy only on success.

    A serialized or direct-constructed admission DTO has no execution authority:
    callers must supply the current policy, registry, and frozen runtime policy
    at the point where they need to use it.
    """
    admission = admit_approved_route_plan(policy, registry, runtime_policy)
    if not admission.admitted:
        raise ValueError(f"frozen multi-route policy was not admitted: {admission.disposition.value}")
    return runtime_policy
