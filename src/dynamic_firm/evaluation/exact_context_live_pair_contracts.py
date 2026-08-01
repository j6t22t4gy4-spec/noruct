from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from dynamic_firm.runtime.models import to_primitive

from .context_binding import ExactContextBoundExpectedRun
from .eval_contracts import EvaluationTrajectoryProjection
from .firm_value_campaign import FirmValueCampaignEvent
from .information_boundary import (
    InformationBoundaryAdmissionProjection,
    InformationBoundaryArtifactProjection,
    InformationBoundaryCheck,
    InformationBoundaryCostProjection,
    InformationBoundarySafetyProjection,
)

class ExactContextLivePairState(StrEnum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class ExactContextRegressionProbe:
    python_version: str
    passed: bool
    test_count: int
    skipped_count: int
    return_code: int
    output_sha256: str


@dataclass(frozen=True, slots=True)
class ExactContextNaturalEvidence:
    schema_version: str
    content_hash: str
    projection_revision: str
    source_revision: str
    distribution_sha256: str
    noruct_version: str
    regression: ExactContextRegressionProbe
    alpha_schema_version: str
    alpha_report_sha256: str
    alpha_passed_checks: int
    alpha_total_checks: int
    blocking_checks: tuple[str, ...]
    external_model_calls: int
    quota_consumed: bool

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("content_hash", None)
        return payload


@dataclass(frozen=True, slots=True)
class ExactContextLivePairManifest:
    schema_version: str
    pair_id: str
    content_hash: str
    created_at: str
    expires_at: str
    noruct_version: str
    binding_id: str
    binding_content_hash: str
    preparation_id: str
    preparation_content_hash: str
    source_revision: str
    distribution_sha256: str
    provider_kind: str
    model_id: str
    authority_profile: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    goal_digest: str
    production_context_fingerprint: str
    parent_extension_id: str
    parent_pattern_id: str
    parent_semantic_anchor: str
    parent_company_state_sha256: str
    bound_pattern_id: str
    natural_evidence_content_hash: str
    completion_contract_revision: str
    completion_validator_revision: str
    max_model_calls_per_run: int
    max_model_calls_pair: int
    max_input_tokens_per_run: int
    max_output_tokens_per_run: int
    max_cost_usd_per_run: float
    max_wall_time_ms_per_run: int
    expected_runs: tuple[ExactContextBoundExpectedRun, ...]
    automatic_approval: bool
    eligible_for_apply: bool
    # Keep historical Native Runtime evidence decodable while binding newly
    # prepared pairs to their selected Employee Execution Port.
    employee_runtime: str = "native"

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("pair_id", None)
        payload.pop("content_hash", None)
        return payload


@dataclass(frozen=True, slots=True)
class ExactContextLivePairPreflight:
    schema_version: str
    preflight_id: str
    content_hash: str
    recorded_at: str
    noruct_version: str
    pair_id: str
    source_revision: str
    distribution_sha256: str
    binding_content_hash: str
    preparation_content_hash: str
    natural_evidence_content_hash: str
    model_id: str
    ready: bool
    checks: tuple[InformationBoundaryCheck, ...]
    external_model_calls: int
    quota_consumed: bool

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("preflight_id", None)
        payload.pop("content_hash", None)
        return payload


@dataclass(frozen=True, slots=True)
class ExactContextValidationProjection:
    passed: bool
    attempt_count: int
    failed_checks: tuple[str, ...]
    repair_used: bool
    capability_signal_match: bool
    review_basis_match: bool
    no_memory_identifier_leak: bool


@dataclass(frozen=True, slots=True)
class ExactContextLiveRecord:
    schema_version: str
    evidence_class: str
    evidence_id: str
    content_hash: str
    recorded_at: str
    noruct_version: str
    pair_id: str
    binding_id: str
    binding_content_hash: str
    preparation_id: str
    preparation_content_hash: str
    source_revision: str
    distribution_sha256: str
    provider_kind: str
    model_id: str
    authority_profile: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    goal_digest: str
    production_context_fingerprint: str
    bound_pattern_id: str
    natural_evidence_content_hash: str
    run_id: str
    workload_hash: str
    strategy: str
    prior_source: str
    prior_pattern_ids: tuple[str, ...]
    status: str
    task_success: bool
    artifact: InformationBoundaryArtifactProjection
    safety: InformationBoundarySafetyProjection
    admission: InformationBoundaryAdmissionProjection
    cost: InformationBoundaryCostProjection
    trajectory: EvaluationTrajectoryProjection
    validation: ExactContextValidationProjection
    prior_exposed_ids: tuple[str, ...]
    prior_aligned_ids: tuple[str, ...]
    no_gap_control_exposed: bool
    no_gap_control_aligned: bool
    provider_request_refs: tuple[str, ...]
    configured_model_call_limit: int
    configured_input_token_limit: int
    configured_output_token_limit: int
    configured_cost_limit_usd: float
    configured_wall_time_ms: int
    elapsed_ms: int
    external_model_calls: int
    quota_confirmed: bool
    automatic_approval: bool
    eligible_for_apply: bool

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("evidence_id", None)
        payload.pop("content_hash", None)
        return payload


@dataclass(frozen=True, slots=True)
class ExactContextLivePairStatus:
    schema_version: str
    pair_id: str
    state: ExactContextLivePairState
    manifest_content_hash: str
    manifest_fresh: bool
    viable: bool
    stop_reason: str | None
    completed_runs: int
    expected_runs: int
    failed_runs: int
    interrupted_runs: int
    next_slot: str | None
    next_strategy: str | None
    max_model_calls_for_next_run: int
    max_wall_time_ms_for_next_run: int
    explicit_quota_confirmation_required: bool
    external_model_calls_recorded: int
    event_count: int
    ledger_verified: bool
    record_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExactContextLivePairPreparation:
    preflight: ExactContextLivePairPreflight
    status: ExactContextLivePairStatus


@dataclass(frozen=True, slots=True)
class ExactContextLivePairRunResult:
    event: FirmValueCampaignEvent
    status: ExactContextLivePairStatus
    record_path: str | None
    task_success: bool


@dataclass(frozen=True, slots=True)
class ExactContextLivePairComparison:
    schema_version: str
    pair_id: str
    manifest_content_hash: str
    completed_runs: int
    expected_runs: int
    control_quality: float
    candidate_quality: float
    quality_gain: float
    control_model_calls: int
    candidate_model_calls: int
    model_call_delta: int
    control_repairs: int
    candidate_repairs: int
    repair_delta: int
    control_tokens: int
    candidate_tokens: int
    token_delta: int
    safety_gate_passed: bool
    attribution_gate_passed: bool
    budget_gate_passed: bool
    effect_gate_passed: bool
    pair_gate_passed: bool
    proposal_recommended: bool
    automatic_approval: bool
    eligible_for_apply: bool
    outcome: str
    recommended_direction: str
    checks: tuple[InformationBoundaryCheck, ...]
    aggregator_provider_calls: int
    aggregator_quota_consumed: bool


