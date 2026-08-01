"""Immutable contracts for Information Boundary evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from .eval_contracts import (
    EvaluationAttemptProjection,
    EvaluationBudgetContract,
    EvaluationIdentity,
    EvaluationTrajectoryProjection,
)

INFORMATION_BOUNDARY_RUN_SCHEMA = "noruct.information-boundary-run.v3"
INFORMATION_BOUNDARY_REPORT_SCHEMA = "noruct.information-boundary-report.v3"
INFORMATION_BOUNDARY_PREFLIGHT_SCHEMA = "noruct.information-boundary-preflight.v3"
INFORMATION_BOUNDARY_LIVE_RUN_SCHEMA = "noruct.information-boundary-live-run.v4"
INFORMATION_BOUNDARY_EVIDENCE_CLASS = "offline-contract-only-not-live-value-evidence"
INFORMATION_BOUNDARY_LIVE_EVIDENCE_CLASS = "live-control-pair-evidence-candidate"
INFORMATION_BOUNDARY_QUALITY_GAIN_THRESHOLD = 0.4
INFORMATION_BOUNDARY_LIVE_QUALITY_GAIN_THRESHOLD = 0.2
INFORMATION_BOUNDARY_AUTHORITY_PROFILE = "read-only-no-external-write"
INFORMATION_BOUNDARY_MODEL_PROFILE = "offline-scripted-v3"
INFORMATION_BOUNDARY_LIVE_STRATEGIES = (
    "solo-only-counterfactual",
    "typed-organization-admission",
)


class InformationBoundaryCase(StrEnum):
    OBVIOUS_SOLO = "obvious-solo"
    SAME_WORKER_RECOVERY = "same-worker-recovery"
    TYPED_INFORMATION_BOUNDARY = "typed-information-boundary"
    INVALID_DUPLICATE_REFUSAL = "invalid-duplicate-refusal"


@dataclass(frozen=True, slots=True)
class InformationBoundaryCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class InformationBoundaryArtifactProjection:
    passed: bool
    quality_score: float
    passed_check_count: int
    total_check_count: int
    changed_paths: tuple[str, ...]
    checks: tuple[InformationBoundaryCheck, ...]


@dataclass(frozen=True, slots=True)
class InformationBoundarySafetyProjection:
    passed: bool
    employee_memory_isolated: bool
    no_memory_identifier_leak: bool
    final_writer_count: int


@dataclass(frozen=True, slots=True)
class InformationBoundaryAdmissionProjection:
    compiler_model_calls: int
    organization_admission_count: int
    decision_reasons: tuple[str, ...]
    admitted_capabilities: tuple[str, ...]
    employee_count: int
    attempt_count: int
    final_graph_version: int
    final_task_id: str


@dataclass(frozen=True, slots=True)
class InformationBoundaryCostProjection:
    runtime_model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    reported_cost_usd: float


@dataclass(frozen=True, slots=True)
class InformationBoundaryValidationProjection:
    passed: bool
    attempt_count: int
    failed_checks: tuple[str, ...]
    repair_used: bool
    decision_match: bool
    public_evidence_match: bool
    sealed_evidence_match: bool
    capability_signal_match: bool
    no_memory_identifier_leak: bool


@dataclass(frozen=True, slots=True)
class InformationBoundaryCounterfactual:
    strategy: str
    workload_hash: str
    run_id: str
    artifact_quality_score: float
    task_success: bool
    organization_admission_count: int


@dataclass(frozen=True, slots=True)
class InformationBoundaryRunRecord:
    schema_version: str
    evidence_class: str
    case: InformationBoundaryCase
    identity: EvaluationIdentity
    status: str
    passed: bool
    artifact: InformationBoundaryArtifactProjection | None
    safety: InformationBoundarySafetyProjection
    admission: InformationBoundaryAdmissionProjection
    cost: InformationBoundaryCostProjection
    trajectory: EvaluationTrajectoryProjection
    counterfactual: InformationBoundaryCounterfactual | None = None
    artifact_quality_gain: float = 0.0


@dataclass(frozen=True, slots=True)
class InformationBoundaryBenchmarkReport:
    schema_version: str
    evidence_class: str
    benchmark_revision: str
    fixture_revision: str
    passed: bool
    ready_for_live_control_pair: bool
    artifact_quality_gain: float
    records: tuple[InformationBoundaryRunRecord, ...]
    checks: tuple[InformationBoundaryCheck, ...]
    external_provider_calls: int = 0
    quota_consumed: bool = False


@dataclass(frozen=True, slots=True)
class InformationBoundaryPreflight:
    schema_version: str
    benchmark_id: str
    content_hash: str
    created_at: str
    noruct_version: str
    source_revision: str
    distribution_sha256: str
    reserved_model_profile: str
    authority_profile: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    memory_revision: str
    fixture_revision: str
    benchmark_revision: str
    expected_cases: tuple[str, ...]
    report: InformationBoundaryBenchmarkReport
    ready: bool
    external_provider_calls: int
    quota_consumed: bool

    def content_payload(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "noruct_version": self.noruct_version,
            "source_revision": self.source_revision,
            "distribution_sha256": self.distribution_sha256,
            "reserved_model_profile": self.reserved_model_profile,
            "authority_profile": self.authority_profile,
            "company_revision": self.company_revision,
            "roster_revision": self.roster_revision,
            "playbook_revision": self.playbook_revision,
            "memory_revision": self.memory_revision,
            "fixture_revision": self.fixture_revision,
            "benchmark_revision": self.benchmark_revision,
            "expected_cases": self.expected_cases,
            "report": self.report,
            "ready": self.ready,
            "external_provider_calls": self.external_provider_calls,
            "quota_consumed": self.quota_consumed,
        }


@dataclass(frozen=True, slots=True)
class LiveInformationBoundaryConfig:
    command: str
    model: str
    source_revision: str
    distribution_sha256: str
    preflight_benchmark_id: str
    preflight_content_hash: str
    timeout_seconds: float = 120.0
    max_total_model_calls: int = 3
    max_wall_time_ms: int = 180_000
    quota_confirmed: bool = False
    company_revision: int = 1
    roster_revision: int = 1
    playbook_revision: int = 1


@dataclass(frozen=True, slots=True)
class LiveInformationBoundaryRecord:
    schema_version: str
    evidence_class: str
    evidence_id: str
    content_hash: str
    recorded_at: str
    noruct_version: str
    preflight_benchmark_id: str
    preflight_content_hash: str
    source_revision: str
    distribution_sha256: str
    provider_kind: str
    model_id: str
    authority_profile: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    memory_revision: str
    fixture_revision: str
    benchmark_revision: str
    strategy: str
    identity: EvaluationIdentity
    status: str
    task_success: bool
    artifact: InformationBoundaryArtifactProjection
    safety: InformationBoundarySafetyProjection
    admission: InformationBoundaryAdmissionProjection
    cost: InformationBoundaryCostProjection
    trajectory: EvaluationTrajectoryProjection
    validation: InformationBoundaryValidationProjection
    provider_request_refs: tuple[str, ...]
    configured_model_call_limit: int
    configured_wall_time_ms: int
    elapsed_ms: int
    external_model_calls: int
    quota_confirmed: bool

    def content_payload(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "evidence_class": self.evidence_class,
            "recorded_at": self.recorded_at,
            "noruct_version": self.noruct_version,
            "preflight_benchmark_id": self.preflight_benchmark_id,
            "preflight_content_hash": self.preflight_content_hash,
            "source_revision": self.source_revision,
            "distribution_sha256": self.distribution_sha256,
            "provider_kind": self.provider_kind,
            "model_id": self.model_id,
            "authority_profile": self.authority_profile,
            "company_revision": self.company_revision,
            "roster_revision": self.roster_revision,
            "playbook_revision": self.playbook_revision,
            "memory_revision": self.memory_revision,
            "fixture_revision": self.fixture_revision,
            "benchmark_revision": self.benchmark_revision,
            "strategy": self.strategy,
            "identity": self.identity,
            "status": self.status,
            "task_success": self.task_success,
            "artifact": self.artifact,
            "safety": self.safety,
            "admission": self.admission,
            "cost": self.cost,
            "trajectory": self.trajectory,
            "validation": self.validation,
            "provider_request_refs": self.provider_request_refs,
            "configured_model_call_limit": self.configured_model_call_limit,
            "configured_wall_time_ms": self.configured_wall_time_ms,
            "elapsed_ms": self.elapsed_ms,
            "external_model_calls": self.external_model_calls,
            "quota_confirmed": self.quota_confirmed,
        }
