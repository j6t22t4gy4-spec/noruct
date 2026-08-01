from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    ActionPolicy,
    EmployeeCapabilityProfile,
    EmployeeSessionRetention,
    TaskEvidencePack,
    VersionedContent,
)


EMPLOYEE_CAPABILITY_PROFILE_REVISION = "employee-capability-profile-v1"
EMPLOYEE_EVALUATION_REVISION = "employee-evaluation-v0"


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _content_ref(item: VersionedContent) -> str:
    content_hash = item.content_hash or hashlib.sha256(
        item.content.encode("utf-8")
    ).hexdigest()
    return f"{item.content_id}@{item.revision}#{content_hash}"


def _tool_grant_projection(policy: ActionPolicy) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        {
            "tool_name": grant.tool_name,
            "allowed_effects": tuple(sorted(effect.value for effect in grant.allowed_effects)),
            "resource_patterns": tuple(sorted(set(grant.resource_patterns))),
            "max_calls": grant.max_calls,
            "max_cost_usd": grant.max_cost_usd,
            "requires_approval": grant.requires_approval,
        }
        for grant in sorted(policy.tool_grants, key=lambda item: item.tool_name)
    )


def build_employee_capability_profile(
    *,
    employee_id: str,
    roster_revision: int,
    model_profile: str,
    capabilities: Sequence[str],
    skills: Sequence[VersionedContent],
    action_policy: ActionPolicy,
    task_evidence: TaskEvidencePack | None,
    memory_namespace: str,
    selected_memory: Sequence[VersionedContent],
    session_retention: EmployeeSessionRetention,
    validator_ids: Iterable[str],
    evaluation_revision: str = EMPLOYEE_EVALUATION_REVISION,
) -> EmployeeCapabilityProfile:
    """Freeze the real, content-free execution surface for one EmployeeRun."""

    grants = _tool_grant_projection(action_policy)
    effects = tuple(
        sorted(
            {
                effect
                for grant in grants
                for effect in grant["allowed_effects"]
            }
        )
    )
    permission_projection = {
        "default_decision": action_policy.default_decision.value,
        "network_policy": action_policy.network_policy,
        "filesystem_policy": action_policy.filesystem_policy,
        "sandbox_profile": action_policy.sandbox_profile,
        "approval_grant_count": len(action_policy.approval_grants),
        "secret_ref_count": len(action_policy.secret_refs),
        "tool_grant_digest": _digest(grants),
    }
    knowledge_scopes = ()
    if task_evidence is not None:
        knowledge_scopes = (task_evidence.access_scope,)
    profile = EmployeeCapabilityProfile.create(
        employee_id=employee_id,
        roster_revision=roster_revision,
        model_profile=model_profile,
        capability_ids=capabilities,
        skill_revision_refs=tuple(_content_ref(item) for item in skills),
        tool_names=tuple(grant["tool_name"] for grant in grants),
        tool_grant_digest=_digest(grants),
        permission_effects=effects,
        permission_digest=_digest(permission_projection),
        knowledge_scopes=knowledge_scopes,
        memory_namespace=memory_namespace,
        memory_revision_refs=tuple(_content_ref(item) for item in selected_memory),
        session_policy=session_retention.value,
        validator_ids=tuple(validator_ids),
        evaluation_revision=evaluation_revision,
    )
    profile.verify()
    return profile


EMPLOYEE_MATERIAL_PROFILE_DIMENSIONS = (
    "model_profile",
    "capability_ids",
    "skill_revision_refs",
    "tool_grant_digest",
    "permission_digest",
    "knowledge_scopes",
    "memory_namespace",
    "memory_revision_refs",
    "session_policy",
    "validator_ids",
    "evaluation_revision",
)


def _material_dimension_value(
    profile: EmployeeCapabilityProfile,
    dimension: str,
) -> Any:
    if dimension == "memory_namespace" and not profile.memory_revision_refs:
        return ""
    return getattr(profile, dimension)


def material_profile_difference(
    left: EmployeeCapabilityProfile,
    right: EmployeeCapabilityProfile,
) -> tuple[str, ...]:
    """Name the execution dimensions that make two Employees heterogeneous."""

    left.verify()
    right.verify()
    return tuple(
        dimension
        for dimension in EMPLOYEE_MATERIAL_PROFILE_DIMENSIONS
        if _material_dimension_value(left, dimension)
        != _material_dimension_value(right, dimension)
    )


def materially_equivalent(
    left: EmployeeCapabilityProfile,
    right: EmployeeCapabilityProfile,
) -> bool:
    left.verify()
    right.verify()
    return left.material_digest == right.material_digest


def material_profile_dimension_digests(
    profile: EmployeeCapabilityProfile,
) -> tuple[tuple[str, str], ...]:
    """Hash each substitution-relevant capability dimension separately.

    This lets a data-only staffing planner explain *which boundary* drifted
    without copying Skill, Knowledge, memory, permission, or tool content.
    """

    profile.verify()
    return tuple(
        (dimension, _digest(_material_dimension_value(profile, dimension)))
        for dimension in EMPLOYEE_MATERIAL_PROFILE_DIMENSIONS
    )
