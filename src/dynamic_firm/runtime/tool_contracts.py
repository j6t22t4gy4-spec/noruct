from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .models import (
    ActionPolicy,
    EmployeeCapabilityProfile,
    IdempotencyMode,
    ToolEffect,
    ToolRisk,
    ToolSchema,
)
from .ports import ToolHandler


CAPABILITY_PROJECTION_REVISION = "employee-capability-projection-v1"


def capability_projection(
    policy: ActionPolicy,
    schemas: tuple[ToolSchema, ...],
    *,
    employee_profile: EmployeeCapabilityProfile | None = None,
) -> Mapping[str, Any]:
    """Return content-free evidence for one frozen employee tool surface."""

    canonical = [
        {
            "name": schema.name,
            "description": schema.description,
            "input_schema": schema.input_schema,
        }
        for schema in schemas
    ]
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    projection: dict[str, Any] = {
        "revision": CAPABILITY_PROJECTION_REVISION,
        "tool_count": len(schemas),
        "tool_schema_sha256": hashlib.sha256(encoded).hexdigest(),
        "external_read_enabled": policy.network_policy == "EXTERNAL_READ_ONLY",
        "native_mcp_discovery": False,
        "native_employee_delegation": False,
        "organization_delegation": "typed_run_signal_to_firm_kernel",
    }
    if employee_profile is not None:
        employee_profile.verify()
        projection.update(
            {
                "employee_profile_revision": employee_profile.schema_version,
                "employee_profile_digest": employee_profile.profile_digest,
                "employee_material_digest": employee_profile.material_digest,
            }
        )
    return projection


@dataclass(frozen=True, slots=True)
class CapabilityProjectionAudit:
    """A content-free consistency check for a frozen employee tool surface.

    A configured integration is not enough to make a capability usable.  The
    employee sees a tool only when its definition and its immutable Job grant
    agree.  Keeping this comparison beside :class:`ToolRegistry` makes the
    registry the single execution-side authority and prevents Settings or a
    route-specific launcher from silently advertising a tool the employee
    cannot execute.
    """

    registered_tool_names: tuple[str, ...]
    granted_tool_names: tuple[str, ...]
    exposed_tool_names: tuple[str, ...]
    withheld_tool_names: tuple[str, ...]
    dangling_grant_names: tuple[str, ...]
    effect_mismatch_names: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.dangling_grant_names and not self.effect_mismatch_names

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "valid": self.valid,
            "registered_tool_names": self.registered_tool_names,
            "granted_tool_names": self.granted_tool_names,
            "exposed_tool_names": self.exposed_tool_names,
            "withheld_tool_names": self.withheld_tool_names,
            "dangling_grant_names": self.dangling_grant_names,
            "effect_mismatch_names": self.effect_mismatch_names,
        }
from .ports import ApprovalPort, CancellationToken, OperationCancelled, ToolHandler
from .redaction import redact_prompt_text, redact_tool_output
from .store import ApprovalConflict, RunStore
from .company_coordination import CompanyCoordinationError, RemoteCompanyCoordinationClient


class ToolValidationError(Exception):
    pass


class ToolEffectNotStarted(ToolValidationError):
    """A trusted handler rejected the action before any observable effect.

    Tool implementations may raise this only while they can still prove that
    no WRITE, EXECUTE, or EXTERNAL_COMMUNICATION effect began. Generic
    validation/handler failures after effectful handler entry remain
    indeterminate.
    """


class PolicyDenied(Exception):
    pass


class ToolExecutionError(Exception):
    pass


Validator = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ResourceKey = Callable[[Mapping[str, Any]], str]
ApprovalPreview = Callable[[Mapping[str, Any]], str]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    effect: ToolEffect
    risk: ToolRisk
    idempotency_mode: IdempotencyMode
    validator: Validator
    resource_key: ResourceKey
    handler: ToolHandler
    timeout_ms: int = 5_000
    output_limit_bytes: int = 64_000
    requires_approval: bool = False
    approval_preview: ApprovalPreview | None = None
    allow_session_approval: bool = False
    # Concurrency is opt-in. A low-risk READ effect alone does not prove that
    # the handler or its upstream service is safe to call concurrently.
    parallel_safe: bool = False

    def schema(self) -> ToolSchema:
        return ToolSchema(self.name, self.description, self.input_schema)


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"Tool already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def schemas_for_policy(self, policy: ActionPolicy) -> tuple[ToolSchema, ...]:
        granted = {grant.tool_name: grant for grant in policy.tool_grants}
        return tuple(
            definition.schema()
            for name, definition in sorted(self._definitions.items())
            if name in granted and definition.effect in granted[name].allowed_effects
        )

    def audit_projection(self, policy: ActionPolicy) -> CapabilityProjectionAudit:
        """Compare the actual registry with one immutable ActionPolicy.

        ``withheld`` is informational: a local connector can be configured
        while an explicit global policy deliberately withholds its actions.
        A dangling grant or an effect mismatch is a programming/configuration
        error and must never reach the employee runtime.
        """

        registered = tuple(sorted(self._definitions))
        grants = {grant.tool_name: grant for grant in policy.tool_grants}
        granted = tuple(sorted(grants))
        dangling = tuple(name for name in granted if name not in self._definitions)
        mismatch = tuple(
            name
            for name in granted
            if name in self._definitions
            and self._definitions[name].effect not in grants[name].allowed_effects
        )
        exposed = tuple(
            name
            for name in registered
            if name in grants and self._definitions[name].effect in grants[name].allowed_effects
        )
        withheld = tuple(name for name in registered if name not in exposed)
        return CapabilityProjectionAudit(
            registered_tool_names=registered,
            granted_tool_names=granted,
            exposed_tool_names=exposed,
            withheld_tool_names=withheld,
            dangling_grant_names=dangling,
            effect_mismatch_names=mismatch,
        )
