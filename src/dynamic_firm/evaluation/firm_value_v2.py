from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm import __version__
from dynamic_firm.coding import CodingWorkResult, ValidationAttempt
from dynamic_firm.coding.ports import CodingValidatorPort
from dynamic_firm.company.models import content_digest
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.runtime.models import (
    CompletionEnvelope,
    ModelResponse,
    StructuredOutputResponse,
    Usage,
    to_primitive,
    utc_now,
)
from dynamic_firm.runtime.redaction import redact_prompt_text

from .closed_loop import (
    CodingStrategyKind,
    _ForcedPlanProvider,
    _run_materialized_evaluation,
)
from .coding import (
    CodingFixtureKind,
    CodingTrajectory,
    ValidationCheck,
    materialize_fixture,
    validate_fixture_candidate,
)
from .firm_value import _failure_family
from . import firm_value_v2_execution as _firm_value_v2_execution

globals().update(
    {
        name: value
        for name, value in vars(_firm_value_v2_execution).items()
        if not name.startswith("__")
    }
)

_firm_value_v2_fixture_contract_impl = firm_value_v2_fixture_contract


def firm_value_v2_fixture_contract(
    fixture: FirmValueV2FixtureKind | str,
) -> FirmValueV2FixtureContract:
    # Tests and sibling evaluators may inject a sealed fixture root through the
    # historical facade seam. Keep the executor's revision calculation bound
    # to that exact fixture authority.
    _firm_value_v2_execution._fixture_root = _fixture_root
    return _firm_value_v2_fixture_contract_impl(fixture)


async def run_firm_value_v2_case(
    fixture: FirmValueV2FixtureKind | str,
    strategy: CodingStrategyKind | str,
) -> FirmValueV2RunRecord:
    fixture = FirmValueV2FixtureKind(fixture)
    strategy = CodingStrategyKind(strategy)
    if strategy not in {CodingStrategyKind.SOLO, CodingStrategyKind.DYNAMIC}:
        raise ValueError("Firm-value v2 supports only SOLO and DYNAMIC strategies")
    contract = firm_value_v2_fixture_contract(fixture)
    with tempfile.TemporaryDirectory(prefix="noruct-firm-value-v2-") as directory:
        root = Path(directory)
        workspace = materialize_firm_value_v2_fixture(fixture, root / "workspace")
        record = await _run_materialized_evaluation(
            fixture=fixture,
            strategy=strategy,
            root=root,
            workspace=workspace,
            provider=_V2Provider(_plan(fixture, strategy), count_compiler=strategy == CodingStrategyKind.DYNAMIC),
            worker=_V2Worker(fixture, strategy),
            model_profile="offline-scripted-v2",
            run_kind="offline-v2",
            max_total_model_calls=8,
            max_wall_time_ms=10_000,
            roster_override=_roster(fixture, strategy),
            validator_override=_V2Validator(fixture),
            score_candidate_override=lambda candidate, trajectory: score_firm_value_v2_candidate(
                fixture, strategy, candidate, trajectory
            ),
            fixture_revision_override=contract.fixture_revision,
        )
    return _run_record_from_closed_loop(
        record,
        fixture=fixture,
        strategy=strategy,
        evidence_class=FIRM_VALUE_V2_EVIDENCE_CLASS,
    )


async def run_live_firm_value_v2_evaluation(
    config: LiveFirmValueV2Config,
    fixture: FirmValueV2FixtureKind | str,
    strategy: CodingStrategyKind | str,
    *,
    provider_factory=None,
    coding_worker_factory=None,
) -> LiveFirmValueV2Record:
    from dynamic_firm.providers.codex_exec import (
        CodexExecCodingWorker,
        CodexExecProvider,
        CodexExecProviderConfig,
    )

    fixture = FirmValueV2FixtureKind(fixture)
    strategy = CodingStrategyKind(strategy)
    if strategy not in {CodingStrategyKind.SOLO, CodingStrategyKind.DYNAMIC}:
        raise ValueError("Firm-value v2 live evaluation supports only SOLO and DYNAMIC")
    if not config.command.strip() or not config.model.strip():
        raise ValueError("Firm-value v2 live evaluation requires command and explicit model")
    if not config.source_revision.strip() or len(config.distribution_sha256) != 64:
        raise ValueError("Firm-value v2 live evaluation requires frozen source and wheel")
    if not config.quota_confirmed:
        raise ValueError("Firm-value v2 live evaluation requires quota confirmation")
    if not config.evaluator_risk_confirmed:
        raise ValueError("Firm-value v2 live evaluation requires evaluator-risk confirmation")
    if not 4 <= config.max_total_model_calls <= 8:
        raise ValueError("Firm-value v2 live model-call limit must be between 4 and 8")
    if config.timeout_seconds <= 0 or not 1_000 <= config.max_wall_time_ms <= 600_000:
        raise ValueError("Firm-value v2 live time limits are outside the bounded contract")
    revisions = (
        config.company_revision,
        config.roster_revision,
        config.playbook_revision,
    )
    if any(type(value) is not int or value < 0 for value in revisions):
        raise ValueError("Firm-value v2 live revisions must be non-negative integers")
    if any(character not in "0123456789abcdef" for character in config.distribution_sha256):
        raise ValueError("Firm-value v2 live wheel SHA-256 is invalid")

    contract = firm_value_v2_fixture_contract(fixture)
    recorded_at = utc_now().isoformat()
    evaluation_run_id = f"firm-value-v2-live-{uuid.uuid4().hex}"
    started = time.monotonic()
    make_provider = provider_factory or CodexExecProvider
    make_worker = coding_worker_factory or CodexExecCodingWorker
    with tempfile.TemporaryDirectory(prefix="noruct-firm-value-v2-live-") as directory:
        root = Path(directory)
        workspace = materialize_firm_value_v2_fixture(fixture, root / "workspace")
        provider_config = CodexExecProviderConfig(
            workspace=workspace,
            command=config.command,
            model=config.model,
            timeout_seconds=config.timeout_seconds,
        )
        live_provider = make_provider(provider_config)
        worker = make_worker(provider_config)
        compiler_provider = (
            live_provider
            if strategy == CodingStrategyKind.DYNAMIC
            else _ForcedPlanProvider(live_provider, _plan(fixture, strategy))
        )
        closed_loop = await _run_materialized_evaluation(
            fixture=fixture,
            strategy=strategy,
            root=root,
            workspace=workspace,
            provider=compiler_provider,
            worker=worker,
            model_profile=config.model,
            run_kind="live-v2",
            max_total_model_calls=config.max_total_model_calls,
            max_wall_time_ms=config.max_wall_time_ms,
            company_revision=config.company_revision,
            roster_revision=config.roster_revision,
            playbook_revision=config.playbook_revision,
            distribution_sha256=config.distribution_sha256,
            roster_override=_roster(fixture, strategy),
            validator_override=_V2Validator(fixture),
            score_candidate_override=lambda candidate, trajectory: score_firm_value_v2_candidate(
                fixture, strategy, candidate, trajectory
            ),
            fixture_revision_override=contract.fixture_revision,
        )
    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    counterfactual_adjustment = 1 if strategy == CodingStrategyKind.SOLO else 0
    result = _run_record_from_closed_loop(
        closed_loop,
        fixture=fixture,
        strategy=strategy,
        evidence_class=FIRM_VALUE_V2_LIVE_EVIDENCE_CLASS,
        runtime_model_call_adjustment=counterfactual_adjustment,
        measured_elapsed_ms=elapsed_ms,
    )
    payload = {
        "schema_version": FIRM_VALUE_V2_LIVE_SCHEMA,
        "recorded_at": recorded_at,
        "noruct_version": __version__,
        "source_revision": config.source_revision,
        "distribution_sha256": config.distribution_sha256,
        "evaluation_run_id": evaluation_run_id,
        "provider_kind": "openai-codex-user-managed",
        "model_id": config.model,
        "planner_source": (
            "live-dynamic-workflow-compiler"
            if strategy == CodingStrategyKind.DYNAMIC
            else "bounded-counterfactual-plan"
        ),
        "company_revision": config.company_revision,
        "roster_revision": config.roster_revision,
        "playbook_revision": config.playbook_revision,
        "permission_mode": "shadow-workspace-approved",
        "approval_mode": "allow-once",
        "configured_model_call_limit": config.max_total_model_calls,
        "configured_wall_time_ms": config.max_wall_time_ms,
        "quota_confirmed": True,
        "evaluator_risk_confirmed": True,
        "evaluator_profile": FIRM_VALUE_V2_EVALUATOR_PROFILE,
        "elapsed_ms": elapsed_ms,
        "external_model_calls": result.cost.runtime_model_calls,
        "result": result,
    }
    digest = content_digest(payload)
    return LiveFirmValueV2Record(
        evidence_id=f"firm-value-v2-live-evidence-{digest[:24]}",
        content_hash=digest,
        **payload,
    )


async def run_firm_value_v2_matrix() -> tuple[FirmValueV2RunRecord, ...]:
    records: list[FirmValueV2RunRecord] = []
    for fixture in FirmValueV2FixtureKind:
        for strategy in (CodingStrategyKind.SOLO, CodingStrategyKind.DYNAMIC):
            records.append(await run_firm_value_v2_case(fixture, strategy))
    return tuple(records)


def compare_firm_value_v2_records(
    records: tuple[FirmValueV2RunRecord, ...],
) -> FirmValueV2Report:
    expected = {
        (fixture, strategy)
        for fixture in FirmValueV2FixtureKind
        for strategy in (CodingStrategyKind.SOLO, CodingStrategyKind.DYNAMIC)
    }
    evidence_classes = {record.evidence_class for record in records}
    if len(evidence_classes) != 1 or not evidence_classes.issubset(
        {FIRM_VALUE_V2_EVIDENCE_CLASS, FIRM_VALUE_V2_LIVE_EVIDENCE_CLASS}
    ):
        raise ValueError("Firm-value v2 comparator refuses mixed evidence classes")
    evidence_class = next(iter(evidence_classes))
    schema_versions = {record.schema_version for record in records}
    if not schema_versions.issubset(
        {FIRM_VALUE_V2_RUN_SCHEMA, FIRM_VALUE_V2_LEGACY_RUN_SCHEMA}
    ):
        raise ValueError("Firm-value v2 comparator refuses non-v2 run schemas")
    if len(schema_versions) != 1:
        raise ValueError("Firm-value v2 comparator refuses mixed run schemas")
    by_key: dict[tuple[FirmValueV2FixtureKind, CodingStrategyKind], FirmValueV2RunRecord] = {}
    for record in records:
        key = (record.fixture, record.strategy)
        if key in by_key:
            raise ValueError(f"Duplicate firm-value v2 run: {record.fixture.value}/{record.strategy.value}")
        by_key[key] = record
    if set(by_key) != expected:
        raise ValueError("Firm-value v2 comparator requires the exact 4x2 run matrix")
    pairs: list[FirmValueV2PairResult] = []
    for fixture in FirmValueV2FixtureKind:
        solo = by_key[(fixture, CodingStrategyKind.SOLO)]
        dynamic = by_key[(fixture, CodingStrategyKind.DYNAMIC)]
        purpose = fixture_purpose(fixture)
        delta = round(dynamic.artifact.quality_score - solo.artifact.quality_score, 4)
        included = purpose == FixturePurpose.VALUE_IDENTIFIABLE
        value_signal = (
            included
            and solo.safety.passed
            and dynamic.safety.passed
            and dynamic.task_success
            and delta >= QUALITY_GAIN_THRESHOLD
        )
        safety_passed = solo.safety.passed and dynamic.safety.passed
        organization_observed = solo.organization.observed and dynamic.organization.observed
        if not included:
            classification = (
                "CONTROL_PASSED"
                if safety_passed and solo.task_success and dynamic.task_success
                else "CONTROL_FAILED"
            )
        else:
            classification = "VALUE_GAIN" if value_signal else "VALUE_NOT_IDENTIFIED"
        pairs.append(
            FirmValueV2PairResult(
                fixture=fixture,
                purpose=purpose,
                solo_task_success=solo.task_success,
                dynamic_task_success=dynamic.task_success,
                solo_artifact_quality=solo.artifact.quality_score,
                dynamic_artifact_quality=dynamic.artifact.quality_score,
                artifact_quality_delta=delta,
                safety_passed=safety_passed,
                organization_observed=organization_observed,
                included_in_gain_denominator=included,
                value_signal=value_signal,
                runtime_model_call_delta=(
                    dynamic.cost.runtime_model_calls - solo.cost.runtime_model_calls
                ),
                total_token_delta=dynamic.cost.total_tokens - solo.cost.total_tokens,
                classification=classification,
            )
        )
    value_pairs = tuple(pair for pair in pairs if pair.included_in_gain_denominator)
    safety_gate = all(pair.safety_passed for pair in pairs)
    control_gate = all(
        pair.classification == "CONTROL_PASSED"
        for pair in pairs
        if pair.purpose == FixturePurpose.CONTROL
    )
    organization_gate = all(pair.organization_observed for pair in pairs)
    value_gain_count = sum(1 for pair in value_pairs if pair.value_signal)
    ready = (
        safety_gate
        and control_gate
        and organization_gate
        and value_gain_count == len(value_pairs)
    )
    return FirmValueV2Report(
        schema_version=FIRM_VALUE_V2_REPORT_SCHEMA,
        evidence_class=evidence_class,
        overall_classification=(
            (
                "OFFLINE_CONTRACT_READY_NOT_VALUE_EVIDENCE"
                if ready
                else "OFFLINE_CONTRACT_NOT_READY"
            )
            if evidence_class == FIRM_VALUE_V2_EVIDENCE_CLASS
            else (
                "LIVE_CAMPAIGN_VALUE_GATE_PASSED"
                if ready
                else "LIVE_CAMPAIGN_VALUE_GATE_NOT_MET"
            )
        ),
        ready_for_live_preflight=ready,
        safety_gate_passed=safety_gate,
        control_gate_passed=control_gate,
        organization_gate_passed=organization_gate,
        value_fixture_count=len(value_pairs),
        value_gain_count=value_gain_count,
        pairs=tuple(pairs),
    )


async def run_firm_value_v2_self_test() -> FirmValueV2SelfTestRecord:
    records = await run_firm_value_v2_matrix()
    report = compare_firm_value_v2_records(records)
    signature = inspect.signature(artifact_score_candidate)
    checks = (
        FirmValueV2Check("exact-4x2-matrix", len(records) == 8, f"records={len(records)}"),
        FirmValueV2Check(
            "topology-independent-artifact-scorer",
            tuple(signature.parameters) == ("fixture", "workspace"),
            f"parameters={','.join(signature.parameters)}",
        ),
        FirmValueV2Check(
            "hard-safety-gate",
            report.safety_gate_passed,
            f"passed={report.safety_gate_passed}",
        ),
        FirmValueV2Check(
            "controls-excluded-and-passing",
            report.control_gate_passed
            and sum(1 for pair in report.pairs if not pair.included_in_gain_denominator) == 2,
            "controls=2",
        ),
        FirmValueV2Check(
            "organization-attribution-separated",
            report.organization_gate_passed,
            f"passed={report.organization_gate_passed}",
        ),
        FirmValueV2Check(
            "two-identifiable-value-fixtures",
            report.value_fixture_count == 2 and report.value_gain_count == 2,
            f"gains={report.value_gain_count}/{report.value_fixture_count}",
        ),
        FirmValueV2Check(
            "offline-is-not-live-evidence",
            report.evidence_class == FIRM_VALUE_V2_EVIDENCE_CLASS,
            report.evidence_class,
        ),
        FirmValueV2Check(
            "aggregator-consumes-no-provider-quota",
            report.aggregator_provider_calls == 0 and not report.aggregator_quota_consumed,
            "provider_calls=0 quota_consumed=false",
        ),
    )
    return FirmValueV2SelfTestRecord(
        schema_version=FIRM_VALUE_V2_SELF_TEST_SCHEMA,
        evidence_class=FIRM_VALUE_V2_EVIDENCE_CLASS,
        report=report,
        checks=checks,
        provider_calls=0,
        quota_consumed=False,
    )


def firm_value_v2_to_json(value: object) -> str:
    return json.dumps(to_primitive(value), ensure_ascii=False, sort_keys=True, indent=2)


def _expect_fields(payload: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(payload) != expected:
        raise ValueError(f"{label} has missing or unknown fields")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _non_negative_number(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
    ):
        raise ValueError(f"{label} must be a non-negative number")
    return float(value)


def _string_array(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{label} must contain only non-empty strings")
    return tuple(value)


def _bounded_text_array(
    value: object,
    label: str,
    *,
    max_items: int = 8,
    max_length: int = 512,
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > max_items
        or any(
            not isinstance(item, str) or not item or len(item) > max_length
            for item in value
        )
    ):
        raise ValueError(f"{label} is outside the bounded text-array contract")
    return tuple(value)


def _bounded_text(
    value: object,
    label: str,
    *,
    max_length: int,
) -> str:
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError(f"{label} is outside the bounded text contract")
    return value


def load_firm_value_v2_run_record(
    source: Path | Mapping[str, object],
    *,
    expected_evidence_class: str = FIRM_VALUE_V2_EVIDENCE_CLASS,
) -> FirmValueV2RunRecord:
    payload: Mapping[str, object]
    if isinstance(source, Path):
        payload = _mapping(json.loads(source.read_text(encoding="utf-8")), "Firm-value v2 run")
    else:
        payload = _mapping(source, "Firm-value v2 run")
    schema_version = payload.get("schema_version")
    if schema_version not in {
        FIRM_VALUE_V2_RUN_SCHEMA,
        FIRM_VALUE_V2_LEGACY_RUN_SCHEMA,
    }:
        raise ValueError("Firm-value v2 loader refuses non-v2 run schemas")
    current_schema = schema_version == FIRM_VALUE_V2_RUN_SCHEMA
    _expect_fields(
        payload,
        {
            "schema_version",
            "evidence_class",
            "fixture",
            "purpose",
            "strategy",
            "fixture_revision",
            "status",
            "task_success",
            "artifact",
            "safety",
            "organization",
            "cost",
            *(("diagnostics",) if current_schema else ()),
            "plan_task_ids",
            "plan_dependency_edges",
        },
        "Firm-value v2 run",
    )
    if payload["evidence_class"] != expected_evidence_class:
        raise ValueError("Firm-value v2 run evidence class is invalid")
    fixture = FirmValueV2FixtureKind(str(payload["fixture"]))
    purpose = FixturePurpose(str(payload["purpose"]))
    if purpose != fixture_purpose(fixture):
        raise ValueError("Firm-value v2 run purpose does not match its fixture")
    artifact_payload = _mapping(payload["artifact"], "artifact")
    _expect_fields(
        artifact_payload,
        {
            "passed",
            "exact_checks_passed",
            "requested_change_match",
            "quality_score",
            "passed_check_count",
            "total_check_count",
            "changed_paths",
            "unexpected_paths",
            "validation_command",
            "checks",
        },
        "artifact",
    )
    checks_payload = artifact_payload["checks"]
    if not isinstance(checks_payload, list):
        raise ValueError("artifact checks must be an array")
    checks: list[ValidationCheck] = []
    for item in checks_payload:
        check = _mapping(item, "artifact check")
        _expect_fields(check, {"name", "passed", "message"}, "artifact check")
        checks.append(
            ValidationCheck(
                name=str(check["name"]),
                passed=_boolean(check["passed"], "artifact check passed"),
                message=str(check["message"]),
            )
        )
    quality = artifact_payload["quality_score"]
    if not isinstance(quality, (int, float)) or isinstance(quality, bool) or not 0 <= quality <= 1:
        raise ValueError("artifact quality_score must be between zero and one")
    artifact = ArtifactQualityProjection(
        passed=_boolean(artifact_payload["passed"], "artifact passed"),
        exact_checks_passed=_boolean(
            artifact_payload["exact_checks_passed"], "artifact exact_checks_passed"
        ),
        requested_change_match=_boolean(
            artifact_payload["requested_change_match"], "artifact requested_change_match"
        ),
        quality_score=float(quality),
        passed_check_count=_integer(artifact_payload["passed_check_count"], "passed_check_count"),
        total_check_count=_integer(artifact_payload["total_check_count"], "total_check_count"),
        changed_paths=_string_array(artifact_payload["changed_paths"], "changed_paths"),
        unexpected_paths=(
            ()
            if artifact_payload["unexpected_paths"] == []
            else _string_array(artifact_payload["unexpected_paths"], "unexpected_paths")
        ),
        validation_command=_string_array(
            artifact_payload["validation_command"], "validation_command"
        ),
        checks=tuple(checks),
    )
    if (
        artifact.total_check_count != len(artifact.checks)
        or artifact.passed_check_count != sum(check.passed for check in artifact.checks)
        or artifact.exact_checks_passed != all(check.passed for check in artifact.checks)
        or artifact.passed
        != (artifact.exact_checks_passed and artifact.requested_change_match)
    ):
        raise ValueError("Firm-value v2 artifact projection is internally inconsistent")
    safety_payload = _mapping(payload["safety"], "safety")
    _expect_fields(
        safety_payload,
        {
            "passed",
            "workspace_scope_ok",
            "approval_boundary_ok",
            "at_most_one_writer",
            "validation_consistent",
        },
        "safety",
    )
    safety = SafetyProjection(
        passed=_boolean(safety_payload["passed"], "safety passed"),
        workspace_scope_ok=_boolean(safety_payload["workspace_scope_ok"], "workspace_scope_ok"),
        approval_boundary_ok=_boolean(
            safety_payload["approval_boundary_ok"], "approval_boundary_ok"
        ),
        at_most_one_writer=_boolean(
            safety_payload["at_most_one_writer"], "at_most_one_writer"
        ),
        validation_consistent=_boolean(
            safety_payload["validation_consistent"], "validation_consistent"
        ),
    )
    if safety.passed != all(
        (
            safety.workspace_scope_ok,
            safety.approval_boundary_ok,
            safety.at_most_one_writer,
            safety.validation_consistent,
        )
    ):
        raise ValueError("Firm-value v2 safety projection is internally inconsistent")
    organization_payload = _mapping(payload["organization"], "organization")
    _expect_fields(
        organization_payload,
        {
            "mechanism",
            "observed",
            "employee_count",
            "maximum_parallelism",
            "writer_count",
            "validation_attempt_count",
        },
        "organization",
    )
    organization = OrganizationProjection(
        mechanism=str(organization_payload["mechanism"]),
        observed=_boolean(organization_payload["observed"], "organization observed"),
        employee_count=_integer(organization_payload["employee_count"], "employee_count"),
        maximum_parallelism=_integer(
            organization_payload["maximum_parallelism"], "maximum_parallelism"
        ),
        writer_count=_integer(organization_payload["writer_count"], "writer_count"),
        validation_attempt_count=_integer(
            organization_payload["validation_attempt_count"], "validation_attempt_count"
        ),
    )
    cost_payload = _mapping(payload["cost"], "cost")
    _expect_fields(
        cost_payload,
        (
            {
                "runtime_model_calls",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "reported_cost_usd",
                "measured_elapsed_ms",
            }
            if current_schema
            else {"runtime_model_calls", "total_tokens", "measured_elapsed_ms"}
        ),
        "cost",
    )
    measured_elapsed_ms = cost_payload["measured_elapsed_ms"]
    if measured_elapsed_ms is not None:
        measured_elapsed_ms = _integer(measured_elapsed_ms, "measured_elapsed_ms")
    cost = CostProjection(
        runtime_model_calls=_integer(
            cost_payload["runtime_model_calls"], "runtime_model_calls"
        ),
        input_tokens=(
            _integer(cost_payload["input_tokens"], "input_tokens")
            if current_schema
            else 0
        ),
        output_tokens=(
            _integer(cost_payload["output_tokens"], "output_tokens")
            if current_schema
            else _integer(cost_payload["total_tokens"], "total_tokens")
        ),
        total_tokens=_integer(cost_payload["total_tokens"], "total_tokens"),
        reported_cost_usd=(
            _non_negative_number(cost_payload["reported_cost_usd"], "reported_cost_usd")
            if current_schema
            else 0.0
        ),
        measured_elapsed_ms=measured_elapsed_ms,
    )
    if current_schema and cost.total_tokens != cost.input_tokens + cost.output_tokens:
        raise ValueError("Firm-value v2 token projection is internally inconsistent")
    task_ids = payload["plan_task_ids"]
    edges = payload["plan_dependency_edges"]
    if not isinstance(task_ids, list) or not isinstance(edges, list):
        raise ValueError("Firm-value v2 plan fields must be arrays")
    parsed_task_ids = _string_array(task_ids, "plan_task_ids")
    parsed_edges: list[tuple[str, str]] = []
    for edge in edges:
        if not isinstance(edge, list) or len(edge) != 2:
            raise ValueError("Firm-value v2 dependency edge must contain two task ids")
        parsed_edges.append((str(edge[0]), str(edge[1])))
    task_success = _boolean(payload["task_success"], "task_success")
    if task_success != (artifact.passed and safety.passed):
        raise ValueError("Firm-value v2 task success is inconsistent with its projections")
    if current_schema:
        diagnostic_payload = _mapping(payload["diagnostics"], "diagnostics")
        _expect_fields(
            diagnostic_payload,
            {
                "failure_family",
                "terminal_stage",
                "planning_mode",
                "planning_reason",
                "failure_reason",
                "employee_failure_codes",
                "budget_limit_reasons",
                "worker_attempt_count",
                "validation_attempts",
                "task_terminal_statuses",
                "task_failure_kinds",
            },
            "diagnostics",
        )
        raw_validation_attempts = diagnostic_payload["validation_attempts"]
        if (
            not isinstance(raw_validation_attempts, list)
            or len(raw_validation_attempts) > 8
            or any(type(item) is not bool for item in raw_validation_attempts)
        ):
            raise ValueError("Firm-value v2 diagnostic validation attempts are invalid")
        diagnostics = FailureAttributionProjection(
            failure_family=_bounded_text(
                diagnostic_payload["failure_family"],
                "failure_family",
                max_length=64,
            ),
            terminal_stage=_bounded_text(
                diagnostic_payload["terminal_stage"],
                "terminal_stage",
                max_length=64,
            ),
            planning_mode=_bounded_text(
                diagnostic_payload["planning_mode"],
                "planning_mode",
                max_length=64,
            ),
            planning_reason=_bounded_text(
                diagnostic_payload["planning_reason"],
                "planning_reason",
                max_length=512,
            ),
            failure_reason=_bounded_text(
                diagnostic_payload["failure_reason"],
                "failure_reason",
                max_length=512,
            ),
            employee_failure_codes=_bounded_text_array(
                diagnostic_payload["employee_failure_codes"],
                "employee_failure_codes",
                max_length=128,
            ),
            budget_limit_reasons=_bounded_text_array(
                diagnostic_payload["budget_limit_reasons"],
                "budget_limit_reasons",
                max_length=128,
            ),
            worker_attempt_count=_integer(
                diagnostic_payload["worker_attempt_count"],
                "worker_attempt_count",
            ),
            validation_attempts=tuple(raw_validation_attempts),
            task_terminal_statuses=_bounded_text_array(
                diagnostic_payload["task_terminal_statuses"],
                "task_terminal_statuses",
                max_length=64,
            ),
            task_failure_kinds=_bounded_text_array(
                diagnostic_payload["task_failure_kinds"],
                "task_failure_kinds",
                max_length=128,
            ),
        )
        if diagnostics.worker_attempt_count > 2:
            raise ValueError("Firm-value v2 worker attempt count exceeds the bounded contract")
    else:
        diagnostics = FailureAttributionProjection(
            failure_family="UNKNOWN",
            terminal_stage="LEGACY_DIAGNOSTICS_UNAVAILABLE",
            planning_mode="",
            planning_reason="",
            failure_reason="",
            employee_failure_codes=(),
            budget_limit_reasons=(),
            worker_attempt_count=0,
            validation_attempts=(),
            task_terminal_statuses=(),
            task_failure_kinds=(),
        )
    return FirmValueV2RunRecord(
        schema_version=str(schema_version),
        evidence_class=str(payload["evidence_class"]),
        fixture=fixture,
        purpose=purpose,
        strategy=CodingStrategyKind(str(payload["strategy"])),
        fixture_revision=str(payload["fixture_revision"]),
        status=str(payload["status"]),
        task_success=task_success,
        artifact=artifact,
        safety=safety,
        organization=organization,
        cost=cost,
        diagnostics=diagnostics,
        plan_task_ids=parsed_task_ids,
        plan_dependency_edges=tuple(parsed_edges),
    )


def load_live_firm_value_v2_record(
    source: Path | Mapping[str, object],
) -> LiveFirmValueV2Record:
    payload: Mapping[str, object]
    if isinstance(source, Path):
        payload = _mapping(
            json.loads(source.read_text(encoding="utf-8")),
            "Firm-value v2 live record",
        )
    else:
        payload = _mapping(source, "Firm-value v2 live record")
    live_schema = payload.get("schema_version")
    if live_schema not in {
        FIRM_VALUE_V2_LIVE_SCHEMA,
        FIRM_VALUE_V2_LEGACY_LIVE_SCHEMA,
    }:
        raise ValueError("Firm-value v2 live loader refuses non-v2 live schemas")
    _expect_fields(
        payload,
        {
            "schema_version",
            "evidence_id",
            "content_hash",
            "recorded_at",
            "noruct_version",
            "source_revision",
            "distribution_sha256",
            "evaluation_run_id",
            "provider_kind",
            "model_id",
            "planner_source",
            "company_revision",
            "roster_revision",
            "playbook_revision",
            "permission_mode",
            "approval_mode",
            "configured_model_call_limit",
            "configured_wall_time_ms",
            "quota_confirmed",
            "evaluator_risk_confirmed",
            "evaluator_profile",
            "elapsed_ms",
            "external_model_calls",
            "result",
        },
        "Firm-value v2 live record",
    )
    result = load_firm_value_v2_run_record(
        _mapping(payload["result"], "Firm-value v2 live result"),
        expected_evidence_class=FIRM_VALUE_V2_LIVE_EVIDENCE_CLASS,
    )
    if (
        live_schema == FIRM_VALUE_V2_LIVE_SCHEMA
        and result.schema_version != FIRM_VALUE_V2_RUN_SCHEMA
    ) or (
        live_schema == FIRM_VALUE_V2_LEGACY_LIVE_SCHEMA
        and result.schema_version != FIRM_VALUE_V2_LEGACY_RUN_SCHEMA
    ):
        raise ValueError("Firm-value v2 live and run schema revisions do not match")
    record = LiveFirmValueV2Record(
        schema_version=str(live_schema),
        evidence_id=str(payload["evidence_id"]),
        content_hash=str(payload["content_hash"]),
        recorded_at=str(payload["recorded_at"]),
        noruct_version=str(payload["noruct_version"]),
        source_revision=str(payload["source_revision"]),
        distribution_sha256=str(payload["distribution_sha256"]),
        evaluation_run_id=str(payload["evaluation_run_id"]),
        provider_kind=str(payload["provider_kind"]),
        model_id=str(payload["model_id"]),
        planner_source=str(payload["planner_source"]),
        company_revision=_integer(payload["company_revision"], "company_revision"),
        roster_revision=_integer(payload["roster_revision"], "roster_revision"),
        playbook_revision=_integer(payload["playbook_revision"], "playbook_revision"),
        permission_mode=str(payload["permission_mode"]),
        approval_mode=str(payload["approval_mode"]),
        configured_model_call_limit=_integer(
            payload["configured_model_call_limit"], "configured_model_call_limit"
        ),
        configured_wall_time_ms=_integer(
            payload["configured_wall_time_ms"], "configured_wall_time_ms"
        ),
        quota_confirmed=_boolean(payload["quota_confirmed"], "quota_confirmed"),
        evaluator_risk_confirmed=_boolean(
            payload["evaluator_risk_confirmed"], "evaluator_risk_confirmed"
        ),
        evaluator_profile=str(payload["evaluator_profile"]),
        elapsed_ms=_integer(payload["elapsed_ms"], "elapsed_ms"),
        external_model_calls=_integer(
            payload["external_model_calls"], "external_model_calls"
        ),
        result=result,
    )
    if (
        record.noruct_version != __version__
        or record.provider_kind != "openai-codex-user-managed"
        or not record.model_id.strip()
        or not record.source_revision.startswith("snapshot-sha256:")
        or not record.evaluation_run_id.startswith("firm-value-v2-live-")
        or record.permission_mode != "shadow-workspace-approved"
        or record.approval_mode != "allow-once"
        or not 4 <= record.configured_model_call_limit <= 8
        or not 1_000 <= record.configured_wall_time_ms <= 600_000
        or record.evaluator_profile != FIRM_VALUE_V2_EVALUATOR_PROFILE
        or not record.quota_confirmed
        or not record.evaluator_risk_confirmed
        or record.external_model_calls != record.result.cost.runtime_model_calls
        or record.external_model_calls > record.configured_model_call_limit
        or record.result.cost.measured_elapsed_ms != record.elapsed_ms
        or record.result.fixture_revision
        != firm_value_v2_fixture_contract(record.result.fixture).fixture_revision
        or record.planner_source
        != (
            "live-dynamic-workflow-compiler"
            if record.result.strategy == CodingStrategyKind.DYNAMIC
            else "bounded-counterfactual-plan"
        )
        or len(record.distribution_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in record.distribution_sha256
        )
    ):
        raise ValueError("Firm-value v2 live record violates its frozen runtime contract")
    try:
        datetime.fromisoformat(record.recorded_at)
    except ValueError as exc:
        raise ValueError("Firm-value v2 live record timestamp is invalid") from exc
    expected_hash = content_digest(record.content_payload())
    if (
        record.content_hash != expected_hash
        or record.evidence_id != f"firm-value-v2-live-evidence-{expected_hash[:24]}"
    ):
        raise ValueError("Firm-value v2 live record identity is invalid")
    return record
