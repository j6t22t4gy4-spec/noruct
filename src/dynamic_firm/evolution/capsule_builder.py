"""Build strict, minimized Learning Capsules from typed local evidence.

The builder deliberately does not accept dictionaries or raw ledger payloads.
Its input records contain only bounded identifiers, enums, counters, scores, and
an already-computed source digest.  Prompts, transcripts, code, paths, memory,
credentials, and arbitrary values therefore never cross this module's input
boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any, Mapping

from .service import (
    BLUEPRINT_DELTA_SCHEMA,
    CAPSULE_SCHEMA,
    CAPSULE_SCHEMA_V2,
    EVOLUTION_PROPOSAL_SCHEMA,
    validate_capsule,
)
from .score_contract import evolution_content_digest, validate_evolution_score


CAPSULE_BUILD_PREVIEW_SCHEMA = "noruct.learning-capsule-build-preview.v1"

_SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_PII_OR_SECRET = re.compile(
    r"(?ix)(?:"
    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+|"
    r"\b(?:https?|file)://|"
    r"(?:^|[^a-z0-9])(?:sk-[a-z0-9_-]{8,}|ghp_[a-z0-9]{8,}|"
    r"github_pat_[a-z0-9_]{8,}|xox[abprs]-[a-z0-9-]{8,}|"
    r"akia[0-9a-z]{12,})(?:$|[^a-z0-9])|"
    r"-----begin[ -](?:rsa[ -])?private[ -]key-----|"
    r"\b(?:authorization|password|passwd|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret)\s*[:=]|"
    r"\b(?:\+?[0-9][0-9 .()-]{8,}[0-9])\b|"
    r"(?:^|[\s])(?:/users/|/home/|[a-z]:\\|~/)"
    r")"
)
_SECRET_IDENTIFIER_SEGMENT = re.compile(
    r"(?:^|[_-])(?:password|passwd|credential|secret|token|api[_-]?key|"
    r"client[_-]?secret)(?:$|[_-])"
)
_OPAQUE_VALUE = re.compile(r"^(?=[a-z0-9]{24,}$)(?=.*[a-z])(?=.*[0-9])[a-z0-9]+$")
_RAW_INPUT_FIELD_NAMES = frozenset(
    {
        "prompt",
        "prompts",
        "message",
        "messages",
        "transcript",
        "transcripts",
        "source_code",
        "code",
        "file_content",
        "file_contents",
        "path",
        "file_path",
        "workspace_path",
        "repository",
        "memory",
        "memories",
        "credential",
        "credentials",
        "secret",
        "secrets",
        "token",
        "tokens",
        "api_key",
        "password",
        "raw_output",
        "raw_value",
    }
)
_EXCLUDED_FIELDS = (
    "prompt",
    "messages",
    "transcript",
    "source_code",
    "file_content",
    "path",
    "memory",
    "credentials",
    "secrets",
    "tokens",
    "api_keys",
    "raw_output",
    "source_record_payload",
)
_BASE_INCLUDED_FIELDS = (
    "schema",
    "capability",
    "authority",
    "task_schema.domain",
    "task_schema.operation",
    "task_schema.input_fields",
    "task_schema.risk_level",
    "execution_summary.workflow_shape",
    "execution_summary.tool_classes",
    "execution_summary.decision_count",
    "execution_summary.redaction_applied",
    "outcome.status",
    "outcome.quality_score",
    "outcome.cost_bucket",
    "outcome.evaluator_kind",
    "outcome.metric_names",
)
_PROPOSAL_INCLUDED_FIELDS = (
    "proposal.schema",
    "proposal.kind",
    "proposal.delta.schema",
    "proposal.delta.blueprint_id",
    "proposal.delta.base_version",
    "proposal.delta.candidate_version",
    "proposal.delta.kind",
    "proposal.delta.alias",
    "proposal.delta.target_capability",
    "proposal.delta.rollback",
)


class CapsuleEvidenceSource(StrEnum):
    COMPANY_EPISODE = "COMPANY_EPISODE"
    ACTIVE_JOB_LEDGER = "ACTIVE_JOB_LEDGER"


class CapsuleAuthority(StrEnum):
    INDIVIDUAL = "individual"
    ORGANIZATION_OWNER = "organization_owner"


class CapsuleRiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class CapsuleOutcomeStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class CapsuleCostBucket(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class CapsuleEvaluatorKind(StrEnum):
    LOCAL_TEST = "LOCAL_TEST"
    USER_REVIEW = "USER_REVIEW"
    OFFLINE_FIXTURE = "OFFLINE_FIXTURE"


class UnsafeCapsuleEvidenceError(ValueError):
    """Raised when typed evidence is malformed or resembles raw/private data."""


def _exact_type(value: object, expected: type[object], name: str) -> None:
    if type(value) is not expected:
        raise UnsafeCapsuleEvidenceError(f"{name} must be an exact {expected.__name__} record")


def _scan_scalar(value: str, name: str) -> None:
    """Reject suspicious values; scanner errors are never treated as safe."""

    try:
        unsafe = (
            _PII_OR_SECRET.search(value) is not None
            or _SECRET_IDENTIFIER_SEGMENT.search(value) is not None
            or _OPAQUE_VALUE.fullmatch(value) is not None
        )
    except Exception as exc:  # pragma: no cover - defensive fail-closed boundary
        raise UnsafeCapsuleEvidenceError(f"{name} could not be safety-scanned") from exc
    if unsafe:
        raise UnsafeCapsuleEvidenceError(f"{name} appears to contain private or secret data")


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise UnsafeCapsuleEvidenceError(
            f"{name} must be a normalized lower-case identifier (2-80 characters)"
        )
    _scan_scalar(value, name)
    return value


def _identifier_tuple(
    value: object,
    name: str,
    *,
    reject_raw_field_names: bool = False,
) -> tuple[str, ...]:
    if type(value) is not tuple or not 1 <= len(value) <= 16:
        raise UnsafeCapsuleEvidenceError(f"{name} must be a tuple with 1 to 16 identifiers")
    normalized = tuple(_identifier(item, name) for item in value)
    if len(set(normalized)) != len(normalized):
        raise UnsafeCapsuleEvidenceError(f"{name} cannot contain duplicate identifiers")
    if reject_raw_field_names:
        rejected = sorted(set(normalized) & _RAW_INPUT_FIELD_NAMES)
        if rejected:
            raise UnsafeCapsuleEvidenceError(
                f"{name} names raw/private field(s) that capsules never accept: "
                + ", ".join(rejected)
            )
    return normalized


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise UnsafeCapsuleEvidenceError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _semver(value: object, name: str) -> str:
    if type(value) is not str or _SEMVER.fullmatch(value) is None:
        raise UnsafeCapsuleEvidenceError(f"{name} must be an exact semantic version")
    return value


@dataclass(frozen=True, slots=True)
class CapsuleTaskEvidence:
    domain: str
    operation: str
    input_fields: tuple[str, ...]
    risk_level: CapsuleRiskLevel

    def __post_init__(self) -> None:
        _identifier(self.domain, "task.domain")
        _identifier(self.operation, "task.operation")
        _identifier_tuple(
            self.input_fields,
            "task.input_fields",
            reject_raw_field_names=True,
        )
        _exact_type(self.risk_level, CapsuleRiskLevel, "task.risk_level")


@dataclass(frozen=True, slots=True)
class CapsuleExecutionEvidence:
    workflow_shape: tuple[str, ...]
    tool_classes: tuple[str, ...]
    decision_count: int

    def __post_init__(self) -> None:
        _identifier_tuple(self.workflow_shape, "execution.workflow_shape")
        _identifier_tuple(self.tool_classes, "execution.tool_classes")
        if type(self.decision_count) is not int or not 0 <= self.decision_count <= 10_000:
            raise UnsafeCapsuleEvidenceError(
                "execution.decision_count must be an integer from 0 to 10000"
            )


@dataclass(frozen=True, slots=True)
class CapsuleOutcomeEvidence:
    status: CapsuleOutcomeStatus
    quality_score: float
    cost_bucket: CapsuleCostBucket
    evaluator_kind: CapsuleEvaluatorKind
    metric_names: tuple[str, ...]

    def __post_init__(self) -> None:
        _exact_type(self.status, CapsuleOutcomeStatus, "outcome.status")
        try:
            validate_evolution_score(self.quality_score, "outcome.quality_score")
        except ValueError as exc:
            raise UnsafeCapsuleEvidenceError(
                "outcome.quality_score must be a finite number from 0 to 1 "
                "using non-negative-zero 0.01 steps"
            ) from exc
        _exact_type(self.cost_bucket, CapsuleCostBucket, "outcome.cost_bucket")
        _exact_type(self.evaluator_kind, CapsuleEvaluatorKind, "outcome.evaluator_kind")
        _identifier_tuple(self.metric_names, "outcome.metric_names")


@dataclass(frozen=True, slots=True)
class ActiveJobCapsuleEvidence:
    """Strict projection of Company/ACTIVE JOB evidence, never the ledger payload."""

    source: CapsuleEvidenceSource
    source_record_digest: str
    capability: str
    authority: CapsuleAuthority
    task: CapsuleTaskEvidence
    execution: CapsuleExecutionEvidence
    outcome: CapsuleOutcomeEvidence

    def __post_init__(self) -> None:
        _exact_type(self.source, CapsuleEvidenceSource, "source")
        _digest(self.source_record_digest, "source_record_digest")
        _identifier(self.capability, "capability")
        _exact_type(self.authority, CapsuleAuthority, "authority")
        _exact_type(self.task, CapsuleTaskEvidence, "task")
        _exact_type(self.execution, CapsuleExecutionEvidence, "execution")
        _exact_type(self.outcome, CapsuleOutcomeEvidence, "outcome")


@dataclass(frozen=True, slots=True)
class BlueprintDeltaProposalEvidence:
    """The only proposal shape currently accepted by the public validator."""

    blueprint_id: str
    base_version: str
    candidate_version: str
    alias: str
    target_capability: str

    def __post_init__(self) -> None:
        _identifier(self.blueprint_id, "proposal.blueprint_id")
        base = _semver(self.base_version, "proposal.base_version")
        candidate = _semver(self.candidate_version, "proposal.candidate_version")
        if base == candidate:
            raise UnsafeCapsuleEvidenceError(
                "proposal.candidate_version must differ from proposal.base_version"
            )
        _identifier(self.alias, "proposal.alias")
        _identifier(self.target_capability, "proposal.target_capability")


def _verify_contract_shape(record: object, expected_fields: tuple[str, ...], name: str) -> None:
    """Fail if a future record revision silently introduces an unreviewed field."""

    actual = tuple(item.name for item in fields(record))
    if actual != expected_fields:
        raise UnsafeCapsuleEvidenceError(
            f"{name} contract fields changed without a capsule allowlist revision"
        )


def build_learning_capsule(
    evidence: ActiveJobCapsuleEvidence,
    proposal: BlueprintDeltaProposalEvidence | None = None,
) -> Mapping[str, Any]:
    """Build and revalidate a v1/v2 capsule without receiving any raw value."""

    _exact_type(evidence, ActiveJobCapsuleEvidence, "evidence")
    # Frozen dataclasses prevent accidental mutation, not adversarial use of
    # object.__setattr__. Re-run every leaf validator at the trust boundary.
    evidence.task.__post_init__()
    evidence.execution.__post_init__()
    evidence.outcome.__post_init__()
    evidence.__post_init__()
    _verify_contract_shape(
        evidence,
        (
            "source",
            "source_record_digest",
            "capability",
            "authority",
            "task",
            "execution",
            "outcome",
        ),
        "evidence",
    )
    _verify_contract_shape(
        evidence.task,
        ("domain", "operation", "input_fields", "risk_level"),
        "task",
    )
    _verify_contract_shape(
        evidence.execution,
        ("workflow_shape", "tool_classes", "decision_count"),
        "execution",
    )
    _verify_contract_shape(
        evidence.outcome,
        ("status", "quality_score", "cost_bucket", "evaluator_kind", "metric_names"),
        "outcome",
    )
    if proposal is not None:
        _exact_type(proposal, BlueprintDeltaProposalEvidence, "proposal")
        proposal.__post_init__()
        _verify_contract_shape(
            proposal,
            (
                "blueprint_id",
                "base_version",
                "candidate_version",
                "alias",
                "target_capability",
            ),
            "proposal",
        )

    capsule: dict[str, Any] = {
        "schema": CAPSULE_SCHEMA_V2 if proposal is not None else CAPSULE_SCHEMA,
        "capability": evidence.capability,
        "authority": evidence.authority.value,
        "task_schema": {
            "domain": evidence.task.domain,
            "operation": evidence.task.operation,
            "input_fields": evidence.task.input_fields,
            "risk_level": evidence.task.risk_level.value,
        },
        "execution_summary": {
            "workflow_shape": evidence.execution.workflow_shape,
            "tool_classes": evidence.execution.tool_classes,
            "decision_count": evidence.execution.decision_count,
            # The projection never accepted raw fields. This flag describes
            # guaranteed minimization, not a best-effort string replacement.
            "redaction_applied": True,
        },
        "outcome": {
            "status": evidence.outcome.status.value,
            "quality_score": float(evidence.outcome.quality_score),
            "cost_bucket": evidence.outcome.cost_bucket.value,
            "evaluator_kind": evidence.outcome.evaluator_kind.value,
            "metric_names": evidence.outcome.metric_names,
        },
    }
    if proposal is not None:
        capsule["proposal"] = {
            "schema": EVOLUTION_PROPOSAL_SCHEMA,
            "kind": "BLUEPRINT_DELTA",
            "delta": {
                "schema": BLUEPRINT_DELTA_SCHEMA,
                "blueprint_id": proposal.blueprint_id,
                "base_version": proposal.base_version,
                "candidate_version": proposal.candidate_version,
                "kind": "CAPABILITY_ALIAS_ADD",
                "alias": proposal.alias,
                "target_capability": proposal.target_capability,
                "rollback": {
                    "kind": "CAPABILITY_ALIAS_REMOVE",
                    "alias": proposal.alias,
                },
            },
        }

    # The established network validator is the final compatibility boundary.
    return validate_capsule(capsule)


def preview_learning_capsule(
    evidence: ActiveJobCapsuleEvidence,
    proposal: BlueprintDeltaProposalEvidence | None = None,
) -> Mapping[str, Any]:
    """Return content-free build evidence; never echo a local ledger value."""

    capsule = build_learning_capsule(evidence, proposal)
    included_fields = _BASE_INCLUDED_FIELDS
    if proposal is not None:
        included_fields += _PROPOSAL_INCLUDED_FIELDS
    return {
        "schema": CAPSULE_BUILD_PREVIEW_SCHEMA,
        "accepted": True,
        "capsule_schema": capsule["schema"],
        "payload_digest": evolution_content_digest(capsule),
        "source_evidence": {
            "kind": evidence.source.value,
            "digest": evidence.source_record_digest,
        },
        "included_fields": included_fields,
        "excluded_fields": _EXCLUDED_FIELDS,
        "redaction_applied": True,
    }


__all__ = (
    "CAPSULE_BUILD_PREVIEW_SCHEMA",
    "ActiveJobCapsuleEvidence",
    "BlueprintDeltaProposalEvidence",
    "CapsuleAuthority",
    "CapsuleCostBucket",
    "CapsuleEvaluatorKind",
    "CapsuleEvidenceSource",
    "CapsuleExecutionEvidence",
    "CapsuleOutcomeEvidence",
    "CapsuleOutcomeStatus",
    "CapsuleRiskLevel",
    "CapsuleTaskEvidence",
    "UnsafeCapsuleEvidenceError",
    "build_learning_capsule",
    "preview_learning_capsule",
)
