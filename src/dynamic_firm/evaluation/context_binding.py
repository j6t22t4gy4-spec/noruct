from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from dynamic_firm.company.models import content_digest
from dynamic_firm.runtime.models import to_primitive


EXACT_CONTEXT_BINDING_SCHEMA = "noruct.exact-context-evidence-binding.v1"
EXACT_CONTEXT_BOUND_PREPARATION_SCHEMA = (
    "noruct.workflow-patch-exact-context-preparation.v1"
)
NATURAL_PREFLIGHT_SCHEMA = "noruct.workflow-patch-natural-workload-preflight.v2"
WORKSPACE_PROJECTION_REVISION = "noruct.workspace-structure.v2"
PREFLIGHT_BINDING_EVIDENCE_CLASS = "PREFLIGHT_BINDING"
_MAX_ARTIFACT_BYTES = 256 * 1024
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_SOURCE_REVISION = re.compile(r"snapshot-sha256:[0-9a-f]{64}")
_WORKSPACE_CONTEXT = re.compile(r"wctx2-[0-9a-f]{24}")


class ExactContextBindingFailureCode(StrEnum):
    ARTIFACT_UNAVAILABLE = "ARTIFACT_UNAVAILABLE"
    ARTIFACT_TOO_LARGE = "ARTIFACT_TOO_LARGE"
    INVALID_JSON = "INVALID_JSON"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    CONTENT_HASH_MISMATCH = "CONTENT_HASH_MISMATCH"
    ID_MISMATCH = "ID_MISMATCH"
    PREFLIGHT_IDENTITY_UNAVAILABLE = "PREFLIGHT_IDENTITY_UNAVAILABLE"
    PREFLIGHT_ROUTE_MISMATCH = "PREFLIGHT_ROUTE_MISMATCH"
    PREFLIGHT_PROVIDER_EVIDENCE_INVALID = "PREFLIGHT_PROVIDER_EVIDENCE_INVALID"
    PREFLIGHT_LINEAGE_INVALID = "PREFLIGHT_LINEAGE_INVALID"
    BINDING_FIELD_INVALID = "BINDING_FIELD_INVALID"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    GOAL_MISMATCH = "GOAL_MISMATCH"
    PROFILE_MISMATCH = "PROFILE_MISMATCH"
    PARENT_MISMATCH = "PARENT_MISMATCH"
    BOUND_PATTERN_COLLISION = "BOUND_PATTERN_COLLISION"


class ExactContextBindingError(ValueError):
    """A stable, path-free refusal at the evidence binding boundary."""

    def __init__(self, code: ExactContextBindingFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ExactContextEvidenceBinding:
    schema_version: str
    binding_id: str
    content_hash: str
    evidence_class: str
    source_preflight_id: str
    source_preflight_content_hash: str
    source_recorded_at: str
    production_context_fingerprint: str
    workspace_projection_revision: str
    workspace_projection_truncated: bool
    execution_profile: str
    source_revision: str
    goal_digest: str
    parent_extension_id: str
    parent_pattern_id: str
    parent_semantic_anchor: str
    evaluation_context_fingerprint: str
    product_route: str
    external_model_calls: int
    quota_consumed: bool

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("binding_id", None)
        payload.pop("content_hash", None)
        return payload


@dataclass(frozen=True, slots=True)
class ExactContextBindingCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class ExactContextBoundExpectedRun:
    slot: str
    strategy: str
    workload_hash: str
    run_id: str


@dataclass(frozen=True, slots=True)
class ExactContextBoundPreparation:
    schema_version: str
    preparation_id: str
    content_hash: str
    noruct_version: str
    evidence_class: str
    binding_id: str
    binding_content_hash: str
    source_revision: str
    goal_digest: str
    execution_profile: str
    production_context_fingerprint: str
    workspace_projection_revision: str
    parent_extension_id: str
    parent_pattern_id: str
    parent_semantic_anchor: str
    bound_pattern_id: str
    campaign_id: str
    expected_runs: tuple[ExactContextBoundExpectedRun, ...]
    eligible_for_apply: bool
    automatic_approval: bool
    external_model_calls: int
    quota_consumed: bool
    checks: tuple[ExactContextBindingCheck, ...]

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("preparation_id", None)
        payload.pop("content_hash", None)
        payload.pop("campaign_id", None)
        return payload


def _read_json_object(path: str | Path) -> dict[str, object]:
    artifact = Path(path).expanduser()
    if not artifact.is_file() or artifact.is_symlink():
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.ARTIFACT_UNAVAILABLE
        )
    try:
        if artifact.stat().st_size > _MAX_ARTIFACT_BYTES:
            raise ExactContextBindingError(
                ExactContextBindingFailureCode.ARTIFACT_TOO_LARGE
            )
        value = json.loads(artifact.read_text(encoding="utf-8"))
    except ExactContextBindingError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.INVALID_JSON
        ) from None
    if not isinstance(value, dict):
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.INVALID_JSON
        )
    return value


def _fullmatch(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _natural_preflight_payload(value: Mapping[str, object]) -> dict[str, object]:
    payload = dict(value)
    payload.pop("preflight_id", None)
    payload.pop("content_hash", None)
    return payload


def _validate_natural_preflight(value: Mapping[str, object]) -> None:
    required_fields = {
        "schema_version",
        "preflight_id",
        "content_hash",
        "recorded_at",
        "source_revision",
        "parent_extension_id",
        "parent_semantic_anchor",
        "applied_pattern_id",
        "applied_context_fingerprint",
        "goal_digest",
        "route",
        "workspace_identity_status",
        "workspace_identity_failure_code",
        "workspace_projection_revision",
        "workspace_projection_truncated",
        "workspace_context_fingerprint",
        "selected_prior_ids",
        "ready_for_live_observation",
        "outcome",
        "external_model_calls",
        "quota_consumed",
        "checks",
    }
    if not required_fields.issubset(value):
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.PREFLIGHT_LINEAGE_INVALID
        )
    if value.get("schema_version") != NATURAL_PREFLIGHT_SCHEMA:
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.UNSUPPORTED_SCHEMA
        )
    digest = content_digest(_natural_preflight_payload(value))
    if value.get("content_hash") != digest:
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.CONTENT_HASH_MISMATCH
        )
    if value.get("preflight_id") != f"workflow-patch-natural-preflight-{digest[:24]}":
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.ID_MISMATCH
        )
    if (
        value.get("workspace_identity_status") != "READY"
        or value.get("workspace_identity_failure_code") is not None
        or value.get("workspace_projection_revision")
        != WORKSPACE_PROJECTION_REVISION
        or not _fullmatch(
            _WORKSPACE_CONTEXT,
            value.get("workspace_context_fingerprint"),
        )
        or type(value.get("workspace_projection_truncated")) is not bool
    ):
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.PREFLIGHT_IDENTITY_UNAVAILABLE
        )
    if value.get("route") != "COMPANY_GOAL":
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.PREFLIGHT_ROUTE_MISMATCH
        )
    if value.get("external_model_calls") != 0 or value.get("quota_consumed") is not False:
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.PREFLIGHT_PROVIDER_EVIDENCE_INVALID
        )
    selected = value.get("selected_prior_ids")
    if (
        value.get("outcome")
        != "NATURAL_WORKLOAD_PREFLIGHT_BLOCKED_BY_PRIOR_CONTEXT"
        or selected not in ([], ())
        or value.get("ready_for_live_observation") is not False
        or not isinstance(value.get("checks"), list)
        or value.get("applied_context_fingerprint")
        == value.get("workspace_context_fingerprint")
    ):
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.PREFLIGHT_LINEAGE_INVALID
        )
    try:
        datetime.fromisoformat(str(value.get("recorded_at")))
    except ValueError:
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.PREFLIGHT_LINEAGE_INVALID
        ) from None
    if not (
        _fullmatch(_SOURCE_REVISION, value.get("source_revision"))
        and _fullmatch(_HEX_64, value.get("goal_digest"))
        and _fullmatch(_IDENTIFIER, value.get("parent_extension_id"))
        and _fullmatch(_IDENTIFIER, value.get("applied_pattern_id"))
        and _fullmatch(_HEX_64, value.get("parent_semantic_anchor"))
        and _fullmatch(
            _IDENTIFIER,
            value.get("applied_context_fingerprint"),
        )
    ):
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.PREFLIGHT_LINEAGE_INVALID
        )


def create_exact_context_evidence_binding(
    preflight_path: str | Path,
    *,
    execution_profile: str,
) -> ExactContextEvidenceBinding:
    value = _read_json_object(preflight_path)
    _validate_natural_preflight(value)
    if execution_profile != "READ_ONLY":
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.BINDING_FIELD_INVALID
        )
    base = ExactContextEvidenceBinding(
        schema_version=EXACT_CONTEXT_BINDING_SCHEMA,
        binding_id="pending",
        content_hash="pending",
        evidence_class=PREFLIGHT_BINDING_EVIDENCE_CLASS,
        source_preflight_id=str(value["preflight_id"]),
        source_preflight_content_hash=str(value["content_hash"]),
        source_recorded_at=str(value["recorded_at"]),
        production_context_fingerprint=str(
            value["workspace_context_fingerprint"]
        ),
        workspace_projection_revision=str(
            value["workspace_projection_revision"]
        ),
        workspace_projection_truncated=bool(
            value["workspace_projection_truncated"]
        ),
        execution_profile=execution_profile,
        source_revision=str(value["source_revision"]),
        goal_digest=str(value["goal_digest"]),
        parent_extension_id=str(value["parent_extension_id"]),
        parent_pattern_id=str(value["applied_pattern_id"]),
        parent_semantic_anchor=str(value["parent_semantic_anchor"]),
        evaluation_context_fingerprint=str(
            value["applied_context_fingerprint"]
        ),
        product_route=str(value["route"]),
        external_model_calls=0,
        quota_consumed=False,
    )
    digest = content_digest(base.content_payload())
    return ExactContextEvidenceBinding(
        **{
            **to_primitive(base),
            "binding_id": f"exact-context-binding-{digest[:24]}",
            "content_hash": digest,
        }
    )


def _validate_binding(binding: ExactContextEvidenceBinding) -> None:
    digest = content_digest(binding.content_payload())
    if binding.schema_version != EXACT_CONTEXT_BINDING_SCHEMA:
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.UNSUPPORTED_SCHEMA
        )
    if binding.content_hash != digest:
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.CONTENT_HASH_MISMATCH
        )
    if binding.binding_id != f"exact-context-binding-{digest[:24]}":
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.ID_MISMATCH
        )
    if not (
        binding.evidence_class == PREFLIGHT_BINDING_EVIDENCE_CLASS
        and _fullmatch(_HEX_64, binding.source_preflight_content_hash)
        and binding.source_preflight_id
        == (
            "workflow-patch-natural-preflight-"
            f"{binding.source_preflight_content_hash[:24]}"
        )
        and _fullmatch(_WORKSPACE_CONTEXT, binding.production_context_fingerprint)
        and binding.workspace_projection_revision
        == WORKSPACE_PROJECTION_REVISION
        and type(binding.workspace_projection_truncated) is bool
        and binding.execution_profile == "READ_ONLY"
        and _fullmatch(_SOURCE_REVISION, binding.source_revision)
        and _fullmatch(_HEX_64, binding.goal_digest)
        and _fullmatch(_IDENTIFIER, binding.parent_extension_id)
        and _fullmatch(_IDENTIFIER, binding.parent_pattern_id)
        and _fullmatch(_HEX_64, binding.parent_semantic_anchor)
        and _fullmatch(_IDENTIFIER, binding.evaluation_context_fingerprint)
        and binding.evaluation_context_fingerprint
        != binding.production_context_fingerprint
        and binding.product_route == "COMPANY_GOAL"
        and binding.external_model_calls == 0
        and binding.quota_consumed is False
    ):
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.BINDING_FIELD_INVALID
        )
    try:
        datetime.fromisoformat(binding.source_recorded_at)
    except ValueError:
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.BINDING_FIELD_INVALID
        ) from None


def load_exact_context_evidence_binding(
    path: str | Path,
) -> ExactContextEvidenceBinding:
    value = _read_json_object(path)
    if value.get("schema_version") != EXACT_CONTEXT_BINDING_SCHEMA:
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.UNSUPPORTED_SCHEMA
        )
    try:
        binding = ExactContextEvidenceBinding(**value)
    except (KeyError, TypeError):
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.BINDING_FIELD_INVALID
        ) from None
    _validate_binding(binding)
    return binding


def create_exact_context_bound_preparation(
    binding: ExactContextEvidenceBinding,
    *,
    noruct_version: str,
    source_revision: str,
    goal_digest: str,
    execution_profile: str,
    parent_extension_id: str,
    parent_pattern_id: str,
    parent_semantic_anchor: str,
    bound_pattern_id: str,
) -> ExactContextBoundPreparation:
    _validate_binding(binding)
    if source_revision != binding.source_revision:
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.SOURCE_MISMATCH
        )
    if goal_digest != binding.goal_digest:
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.GOAL_MISMATCH
        )
    if execution_profile != binding.execution_profile:
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.PROFILE_MISMATCH
        )
    if (
        parent_extension_id != binding.parent_extension_id
        or parent_pattern_id != binding.parent_pattern_id
        or parent_semantic_anchor != binding.parent_semantic_anchor
    ):
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.PARENT_MISMATCH
        )
    if (
        not _fullmatch(_IDENTIFIER, bound_pattern_id)
        or bound_pattern_id == parent_pattern_id
    ):
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.BOUND_PATTERN_COLLISION
        )
    workload_hash = content_digest(
        {
            "schema": EXACT_CONTEXT_BOUND_PREPARATION_SCHEMA,
            "binding_content_hash": binding.content_hash,
            "source_revision": source_revision,
            "goal_digest": goal_digest,
            "execution_profile": execution_profile,
            "parent_semantic_anchor": parent_semantic_anchor,
            "bound_pattern_id": bound_pattern_id,
        }
    )
    expected_runs = tuple(
        ExactContextBoundExpectedRun(
            slot=slot,
            strategy=strategy,
            workload_hash=workload_hash,
            run_id=(
                "exact-context-evaluation-run-"
                + content_digest(
                    {
                        "workload_hash": workload_hash,
                        "slot": slot,
                        "strategy": strategy,
                    }
                )[:24]
            ),
        )
        for slot, strategy in (
            ("control", "exact-context-control"),
            ("candidate", "exact-context-candidate"),
        )
    )
    checks = (
        ExactContextBindingCheck(
            "binding-content-verified",
            True,
            binding.content_hash,
        ),
        ExactContextBindingCheck(
            "source-goal-profile-exact",
            True,
            f"source={source_revision},goal={goal_digest},profile={execution_profile}",
        ),
        ExactContextBindingCheck(
            "immutable-parent-lineage",
            True,
            f"extension={parent_extension_id},anchor={parent_semantic_anchor}",
        ),
        ExactContextBindingCheck(
            "separate-bound-pattern",
            True,
            f"parent={parent_pattern_id},bound={bound_pattern_id}",
        ),
        ExactContextBindingCheck(
            "provider-free-prepare",
            True,
            "external-model-calls=0,quota-consumed=false",
        ),
    )
    base = ExactContextBoundPreparation(
        schema_version=EXACT_CONTEXT_BOUND_PREPARATION_SCHEMA,
        preparation_id="pending",
        content_hash="pending",
        noruct_version=noruct_version,
        evidence_class=PREFLIGHT_BINDING_EVIDENCE_CLASS,
        binding_id=binding.binding_id,
        binding_content_hash=binding.content_hash,
        source_revision=source_revision,
        goal_digest=goal_digest,
        execution_profile=execution_profile,
        production_context_fingerprint=(
            binding.production_context_fingerprint
        ),
        workspace_projection_revision=(
            binding.workspace_projection_revision
        ),
        parent_extension_id=parent_extension_id,
        parent_pattern_id=parent_pattern_id,
        parent_semantic_anchor=parent_semantic_anchor,
        bound_pattern_id=bound_pattern_id,
        campaign_id="pending",
        expected_runs=expected_runs,
        eligible_for_apply=False,
        automatic_approval=False,
        external_model_calls=0,
        quota_consumed=False,
        checks=checks,
    )
    digest = content_digest(base.content_payload())
    return ExactContextBoundPreparation(
        **{
            **to_primitive(base),
            "preparation_id": f"exact-context-preparation-{digest[:24]}",
            "content_hash": digest,
            "campaign_id": f"workflow-patch-exact-context-{digest[:24]}",
            "expected_runs": expected_runs,
            "checks": checks,
        }
    )


def load_exact_context_bound_preparation(
    path: str | Path,
) -> ExactContextBoundPreparation:
    value = _read_json_object(path)
    if value.get("schema_version") != EXACT_CONTEXT_BOUND_PREPARATION_SCHEMA:
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.UNSUPPORTED_SCHEMA
        )
    try:
        expected_runs = tuple(
            ExactContextBoundExpectedRun(**item)
            for item in value["expected_runs"]
        )
        checks = tuple(
            ExactContextBindingCheck(**item) for item in value["checks"]
        )
        preparation = ExactContextBoundPreparation(
            **{
                **{
                    key: item
                    for key, item in value.items()
                    if key not in {"expected_runs", "checks"}
                },
                "expected_runs": expected_runs,
                "checks": checks,
            }
        )
    except (KeyError, TypeError):
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.BINDING_FIELD_INVALID
        ) from None
    digest = content_digest(preparation.content_payload())
    expected_slots = (
        ("control", "exact-context-control"),
        ("candidate", "exact-context-candidate"),
    )
    workload_hash = content_digest(
        {
            "schema": EXACT_CONTEXT_BOUND_PREPARATION_SCHEMA,
            "binding_content_hash": preparation.binding_content_hash,
            "source_revision": preparation.source_revision,
            "goal_digest": preparation.goal_digest,
            "execution_profile": preparation.execution_profile,
            "parent_semantic_anchor": preparation.parent_semantic_anchor,
            "bound_pattern_id": preparation.bound_pattern_id,
        }
    )
    expected_run_ids = tuple(
        "exact-context-evaluation-run-"
        + content_digest(
            {
                "workload_hash": workload_hash,
                "slot": item.slot,
                "strategy": item.strategy,
            }
        )[:24]
        for item in expected_runs
    )
    if (
        preparation.content_hash != digest
        or preparation.preparation_id
        != f"exact-context-preparation-{digest[:24]}"
        or preparation.campaign_id
        != f"workflow-patch-exact-context-{digest[:24]}"
        or tuple((item.slot, item.strategy) for item in expected_runs)
        != expected_slots
        or len({item.run_id for item in expected_runs}) != 2
        or any(item.workload_hash != workload_hash for item in expected_runs)
        or tuple(item.run_id for item in expected_runs) != expected_run_ids
        or preparation.evidence_class != PREFLIGHT_BINDING_EVIDENCE_CLASS
        or not _fullmatch(_IDENTIFIER, preparation.noruct_version)
        or preparation.binding_id
        != f"exact-context-binding-{preparation.binding_content_hash[:24]}"
        or not _fullmatch(_HEX_64, preparation.binding_content_hash)
        or not _fullmatch(_SOURCE_REVISION, preparation.source_revision)
        or not _fullmatch(_HEX_64, preparation.goal_digest)
        or preparation.execution_profile != "READ_ONLY"
        or not _fullmatch(
            _WORKSPACE_CONTEXT,
            preparation.production_context_fingerprint,
        )
        or preparation.workspace_projection_revision
        != WORKSPACE_PROJECTION_REVISION
        or not _fullmatch(_IDENTIFIER, preparation.parent_extension_id)
        or not _fullmatch(_IDENTIFIER, preparation.parent_pattern_id)
        or not _fullmatch(_HEX_64, preparation.parent_semantic_anchor)
        or not _fullmatch(_IDENTIFIER, preparation.bound_pattern_id)
        or preparation.bound_pattern_id == preparation.parent_pattern_id
        or not all(check.passed for check in checks)
        or preparation.eligible_for_apply
        or preparation.automatic_approval
        or preparation.external_model_calls != 0
        or preparation.quota_consumed
    ):
        raise ExactContextBindingError(
            ExactContextBindingFailureCode.CONTENT_HASH_MISMATCH
        )
    return preparation


def exact_context_binding_to_json(
    value: ExactContextEvidenceBinding | ExactContextBoundPreparation,
) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
