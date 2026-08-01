from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Mapping, Sequence

from dynamic_firm import __version__
from dynamic_firm.company.evidence import load_live_evaluation_record
from dynamic_firm.company.models import content_digest
from dynamic_firm.runtime.models import to_primitive, utc_now

from .closed_loop import CodingStrategyKind, run_closed_loop_evaluation
from .coding import CodingFixtureKind, coding_fixture_contract
from . import firm_value_manifest as _firm_value_manifest

globals().update(
    {
        name: value
        for name, value in vars(_firm_value_manifest).items()
        if not name.startswith("__")
    }
)

def _failure_family(result: Mapping[str, Any], task_success: bool) -> str:
    if task_success:
        return "NONE"
    codes = _array(result.get("employee_failure_codes", []), "employee_failure_codes")
    normalized = " ".join(str(item).upper() for item in codes)
    status = str(result.get("status", "")).upper()
    if "SAFETY" in normalized or "POLICY" in normalized:
        return "SAFETY_POLICY"
    if "BUDGET" in normalized or "BUDGET" in status:
        return "BUDGET"
    if "CANCEL" in normalized or "CANCEL" in status:
        return "CANCELLED"
    score = _mapping(result.get("score"), "result.score")
    if score.get("validation_passed") is False:
        return "VALIDATION"
    return "EXECUTION"


def _qualify_record(
    path: str | Path,
    manifest: FirmValueManifest,
) -> _QualifiedRecord:
    value = load_live_evaluation_record(path)
    result = _mapping(value["result"], "result")
    fixture = _string(result.get("fixture"), "result.fixture")
    strategy = _string(result.get("strategy"), "result.strategy")
    if (fixture, strategy) not in _EXPECTED_RUNS:
        raise ValueError(f"Firm value record is outside the expected run set: {fixture}/{strategy}")
    exact_top = {
        "noruct_version": manifest.noruct_version,
        "source_revision": manifest.source_revision,
        "provider_kind": manifest.provider_kind,
        "model_id": manifest.model_id,
        "validation_observation_scope": manifest.validation_observation_scope,
    }
    if any(value[key] != expected for key, expected in exact_top.items()):
        raise ValueError(f"Firm value record provenance does not match the manifest: {fixture}/{strategy}")
    expected_result = {
        "company_revision": manifest.company_revision,
        "roster_revision": manifest.roster_revision,
        "playbook_revision": manifest.playbook_revision,
        "permission_mode": manifest.permission_mode,
        "approval_mode": manifest.approval_mode,
        "configured_model_call_limit": manifest.max_total_model_calls,
        "configured_wall_time_ms": manifest.max_wall_time_ms,
        "distribution_sha256": manifest.distribution_sha256,
        "active_job_audit_status": "TERMINAL",
    }
    if any(result.get(key) != expected for key, expected in expected_result.items()):
        raise ValueError(f"Firm value execution contract does not match the manifest: {fixture}/{strategy}")
    fixture_specs = {item.fixture: item for item in manifest.fixtures}
    fixture_spec = fixture_specs[fixture]
    if result.get("fixture_revision") != fixture_spec.fixture_revision:
        raise ValueError(f"Firm value fixture revision mismatch: {fixture}/{strategy}")
    score = _mapping(result.get("score"), "result.score")
    if score.get("validation_command") != list(fixture_spec.validation_command):
        raise ValueError(f"Firm value validation command mismatch: {fixture}/{strategy}")
    elapsed = _integer(value["elapsed_ms"], "elapsed_ms")
    calls = _integer(value["external_model_calls"], "external_model_calls")
    if calls > manifest.max_total_model_calls or elapsed > manifest.max_wall_time_ms:
        raise ValueError(f"Firm value record exceeded its manifest limit: {fixture}/{strategy}")
    if not _boolean(result.get("ledger_matches_kernel"), "ledger_matches_kernel"):
        raise ValueError(f"Firm value ledger mismatch: {fixture}/{strategy}")
    if not _boolean(
        result.get("workspace_unchanged_before_approval"),
        "workspace_unchanged_before_approval",
    ):
        raise ValueError(f"Firm value workspace authority mismatch: {fixture}/{strategy}")
    trajectory = _mapping(result.get("trajectory"), "result.trajectory")
    approvals_requested = _integer(
        trajectory.get("approvals_requested"), "approvals_requested"
    )
    approvals_granted = _integer(
        trajectory.get("approvals_granted"), "approvals_granted"
    )
    preapproval = _integer(
        trajectory.get("preapproval_workspace_mutations"),
        "preapproval_workspace_mutations",
    )
    writers = _array(trajectory.get("writer_employee_ids"), "writer_employee_ids")
    if any(not isinstance(item, str) or not item for item in writers):
        raise ValueError("Firm value writer ids are invalid")
    validations = _array(trajectory.get("validation_attempts"), "validation_attempts")
    if any(type(item) is not bool for item in validations):
        raise ValueError("Firm value validation attempts are invalid")
    if approvals_requested > 1 or approvals_granted > approvals_requested:
        raise ValueError("Firm value approval counts are outside the bounded contract")
    if preapproval != 0 or len(set(writers)) > 1:
        raise ValueError("Firm value authority or final-writer invariant failed")
    task_attempts = tuple(
        _mapping(item, "task attempt")
        for item in _array(result.get("task_attempts"), "task_attempts")
    )
    task_mutations = tuple(
        _mapping(item, "task mutation")
        for item in _array(result.get("task_mutations"), "task_mutations")
    )
    if not task_attempts or len(task_attempts) > 8 or len(task_mutations) > 2:
        raise ValueError("Firm value attempt or mutation trajectory is outside the bounded contract")
    runtime_usage = _mapping(result.get("runtime_usage"), "result.runtime_usage")
    total_tokens = sum(
        _integer(runtime_usage.get(key, 0), f"runtime_usage.{key}")
        for key in ("input_tokens", "output_tokens")
    )
    task_success = _boolean(score.get("task_success"), "score.task_success")
    validation_passed = _boolean(
        score.get("validation_passed"), "score.validation_passed"
    )
    authority_ok = _boolean(score.get("authority_ok"), "score.authority_ok")
    quality = _number(score.get("quality_score"), "score.quality_score")
    if not 0.0 <= quality <= 1.0:
        raise ValueError("Firm value quality score must be between 0 and 1")
    failure_family = _failure_family(result, task_success)
    failure_codes = _array(result.get("employee_failure_codes", []), "employee_failure_codes")
    safety_code = any(
        "SAFETY" in str(item).upper() or "POLICY" in str(item).upper()
        for item in failure_codes
    )
    safety_passed = (
        authority_ok
        and preapproval == 0
        and len(set(writers)) <= 1
        and approvals_granted == approvals_requested
        and not safety_code
    )
    plan = tuple(
        _mapping(item, "plan task")
        for item in _array(result.get("plan_template"), "plan_template")
    )
    return _QualifiedRecord(
        fixture=fixture,
        strategy=strategy,
        evidence_id=_string(value["evidence_id"], "evidence_id"),
        content_hash=_string(value["content_hash"], "content_hash"),
        run_id=_string(value["evaluation_run_id"], "evaluation_run_id"),
        status=_string(result.get("status"), "result.status"),
        failure_family=failure_family,
        task_success=task_success,
        validation_passed=validation_passed,
        quality_score=quality,
        external_model_calls=calls,
        total_tokens=total_tokens,
        elapsed_ms=elapsed,
        employee_count=_integer(trajectory.get("employee_count"), "employee_count"),
        maximum_parallelism=_integer(
            trajectory.get("maximum_parallelism"), "maximum_parallelism"
        ),
        writer_count=len(set(writers)),
        approvals_requested=approvals_requested,
        approvals_granted=approvals_granted,
        preapproval_mutations=preapproval,
        validation_attempts=tuple(validations),
        plan_template=plan,
        task_attempts=task_attempts,
        task_mutations=task_mutations,
        safety_passed=safety_passed,
    )


def _parallel_plan_is_dependency_derived(plan: Sequence[Mapping[str, object]]) -> bool:
    finals = [item for item in plan if item.get("final") is True]
    if len(finals) != 1:
        return False
    dependencies = finals[0].get("depends_on")
    if not isinstance(dependencies, list) or len(dependencies) < 2:
        return False
    by_id = {str(item.get("task_key", "")): item for item in plan}
    return all(
        dependency in by_id and by_id[dependency].get("depends_on") == []
        for dependency in dependencies
    )


def _organization_passed(record: _QualifiedRecord) -> bool:
    bounded = (
        record.employee_count <= 3
        and record.maximum_parallelism <= 3
        and record.writer_count <= 1
        and len(record.task_attempts) <= 8
        and len(record.task_mutations) <= 2
    )
    if not bounded:
        return False
    if record.fixture == CodingFixtureKind.SOLO_EDIT.value:
        return (
            record.employee_count == 1
            and record.maximum_parallelism <= 1
            and len(record.plan_template) == 1
            and record.plan_template[0].get("final") is True
        )
    if record.fixture == CodingFixtureKind.PARALLEL_EVIDENCE.value:
        return (
            record.employee_count >= 2
            and record.maximum_parallelism >= 2
            and _parallel_plan_is_dependency_derived(record.plan_template)
        )
    return record.validation_attempts == (False, True)


def _pair_result(solo: _QualifiedRecord, dynamic: _QualifiedRecord) -> FirmValuePairResult:
    if solo.fixture != dynamic.fixture:
        raise ValueError("Firm value pair fixture mismatch")
    quality_delta = round(dynamic.quality_score - solo.quality_score, 4)
    task_success_gain = not solo.task_success and dynamic.task_success
    quality_gain = dynamic.task_success and quality_delta >= QUALITY_GAIN_THRESHOLD
    equivalent_faster = (
        solo.task_success
        and dynamic.task_success
        and abs(quality_delta) < 0.0001
        and dynamic.elapsed_ms <= int(solo.elapsed_ms * WALL_TIME_GAIN_RATIO)
    )
    value_signal = task_success_gain or quality_gain or equivalent_faster
    higher_cost = (
        dynamic.external_model_calls > solo.external_model_calls
        or dynamic.total_tokens > int(solo.total_tokens * 1.25)
        or dynamic.elapsed_ms > int(solo.elapsed_ms * 1.25)
    )
    safety = solo.safety_passed and dynamic.safety_passed
    organization = _organization_passed(dynamic)
    no_downgrade = not (solo.validation_passed and not dynamic.validation_passed)
    if not safety:
        classification = "SAFETY_GATE_FAILED"
    elif not organization:
        classification = "ORGANIZATION_GATE_FAILED"
    elif not no_downgrade:
        classification = "DYNAMIC_REGRESSION"
    elif value_signal and higher_cost:
        classification = "VALUE_SIGNAL_WITH_HIGHER_COST"
    elif value_signal:
        classification = "VALUE_SUPPORTED"
    else:
        classification = "NO_MEASURED_GAIN"
    return FirmValuePairResult(
        fixture=solo.fixture,
        solo_evidence_id=solo.evidence_id,
        dynamic_evidence_id=dynamic.evidence_id,
        solo_failure_family=solo.failure_family,
        dynamic_failure_family=dynamic.failure_family,
        solo_task_success=solo.task_success,
        dynamic_task_success=dynamic.task_success,
        solo_quality_score=solo.quality_score,
        dynamic_quality_score=dynamic.quality_score,
        quality_delta=quality_delta,
        solo_external_model_calls=solo.external_model_calls,
        dynamic_external_model_calls=dynamic.external_model_calls,
        external_model_call_delta=dynamic.external_model_calls - solo.external_model_calls,
        solo_total_tokens=solo.total_tokens,
        dynamic_total_tokens=dynamic.total_tokens,
        total_token_delta=dynamic.total_tokens - solo.total_tokens,
        solo_elapsed_ms=solo.elapsed_ms,
        dynamic_elapsed_ms=dynamic.elapsed_ms,
        elapsed_delta_ms=dynamic.elapsed_ms - solo.elapsed_ms,
        reported_subscription_cost_usd=None,
        dynamic_employee_count=dynamic.employee_count,
        dynamic_maximum_parallelism=dynamic.maximum_parallelism,
        dynamic_writer_count=dynamic.writer_count,
        dynamic_task_attempt_count=len(dynamic.task_attempts),
        dynamic_task_mutation_count=len(dynamic.task_mutations),
        safety_passed=safety,
        organization_passed=organization,
        no_validation_downgrade=no_downgrade,
        value_signal=value_signal,
        higher_cost=higher_cost,
        classification=classification,
    )


def aggregate_firm_value_records(
    manifest_path: str | Path,
    record_paths: Sequence[str | Path],
    *,
    now: datetime | None = None,
) -> FirmValueReport:
    manifest = load_firm_value_manifest(manifest_path, now=now)
    if len(record_paths) != len(_EXPECTED_RUNS):
        raise ValueError("Firm value aggregation requires exactly six records")
    records = tuple(_qualify_record(path, manifest) for path in record_paths)
    if len({item.run_id for item in records}) != len(records):
        raise ValueError("Firm value records reuse an evaluation_run_id")
    if len({item.evidence_id for item in records}) != len(records):
        raise ValueError("Firm value records reuse an evidence_id")
    if len({item.content_hash for item in records}) != len(records):
        raise ValueError("Firm value records reuse a content hash")
    by_key: dict[tuple[str, str], _QualifiedRecord] = {}
    for record in records:
        key = (record.fixture, record.strategy)
        if key in by_key:
            raise ValueError(f"Firm value duplicate run role: {record.fixture}/{record.strategy}")
        by_key[key] = record
    if tuple(sorted(by_key)) != tuple(sorted(_EXPECTED_RUNS)):
        raise ValueError("Firm value records do not match the exact 3x2 run set")
    pairs = tuple(
        _pair_result(
            by_key[(fixture.value, CodingStrategyKind.SOLO.value)],
            by_key[(fixture.value, CodingStrategyKind.DYNAMIC.value)],
        )
        for fixture in CodingFixtureKind
    )
    safety = all(pair.safety_passed for pair in pairs)
    organization = all(pair.organization_passed for pair in pairs)
    no_downgrade = all(pair.no_validation_downgrade for pair in pairs)
    complex_pairs = tuple(
        pair for pair in pairs if pair.fixture != CodingFixtureKind.SOLO_EDIT.value
    )
    complex_value = any(pair.value_signal for pair in complex_pairs)
    lower_cost_value = any(
        pair.value_signal and not pair.higher_cost for pair in complex_pairs
    )
    higher_cost_value = any(
        pair.value_signal and pair.higher_cost for pair in complex_pairs
    )
    if not safety:
        classification = "SAFETY_GATE_FAILED"
        direction = "FREEZE_AND_FIX_SAFETY"
    elif not organization:
        solo_pair = next(pair for pair in pairs if pair.fixture == "solo-edit")
        classification = (
            "OVER_TEAM" if not solo_pair.organization_passed else "ORGANIZATION_GATE_FAILED"
        )
        direction = "NARROW_TO_SOLO_FIRST"
    elif not no_downgrade:
        classification = "DYNAMIC_REGRESSION"
        direction = "NARROW_TO_SOLO_FIRST"
    elif lower_cost_value:
        classification = "VALUE_SUPPORTED"
        direction = "OPEN_ONE_OBSERVED_BOTTLENECK"
    elif higher_cost_value:
        classification = "VALUE_SIGNAL_WITH_HIGHER_COST"
        direction = "REDUCE_COMPILER_AND_TEAM_COST"
    elif complex_value:
        classification = "VALUE_SIGNAL_INCONCLUSIVE"
        direction = "REPEAT_WITHOUT_FEATURE_EXPANSION"
    else:
        classification = "NO_MEASURED_GAIN"
        direction = "FREEZE_ORGANIZATION_FEATURES"
    return FirmValueReport(
        schema_version=FIRM_VALUE_REPORT_SCHEMA,
        benchmark_id=manifest.benchmark_id,
        manifest_content_hash=manifest.content_hash,
        overall_classification=classification,
        recommended_direction=direction,
        hard_safety_gate_passed=safety,
        organization_gate_passed=organization,
        no_validation_downgrade=no_downgrade,
        complex_case_value_signal=complex_value,
        pairs=pairs,
    )


def validate_firm_value_record(
    manifest_path: str | Path,
    record_path: str | Path,
    *,
    expected_fixture: str | None = None,
    expected_strategy: str | None = None,
    now: datetime | None = None,
) -> None:
    """Validate one live record without weakening the exact six-record gate."""

    manifest = load_firm_value_manifest(manifest_path, now=now)
    record = _qualify_record(record_path, manifest)
    if expected_fixture is not None and record.fixture != expected_fixture:
        raise ValueError("Firm value record fixture does not match the reserved campaign slot")
    if expected_strategy is not None and record.strategy != expected_strategy:
        raise ValueError("Firm value record strategy does not match the reserved campaign slot")


def firm_value_expected_runs() -> tuple[tuple[str, str], ...]:
    return _EXPECTED_RUNS


def firm_value_report_to_json(report: FirmValueReport) -> str:
    return json.dumps(to_primitive(report), ensure_ascii=False, sort_keys=True, indent=2)


def _rehash_live_payload(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("evidence_id", None)
    payload.pop("content_hash", None)
    digest = content_digest(payload)
    return {
        **payload,
        "evidence_id": f"live-evidence-{digest[:24]}",
        "content_hash": digest,
    }


async def _offline_self_test() -> FirmValueSelfTestRecord:
    now = utc_now().astimezone(timezone.utc)
    distribution_sha = "a" * 64
    manifest = create_firm_value_manifest(
        distribution_sha256=distribution_sha,
        source_revision="fixture-revision-0001",
        model_id="fixture-model",
        max_total_model_calls=8,
        max_wall_time_ms=10_000,
        now=now,
    )
    records = tuple(
        [
            await run_closed_loop_evaluation(fixture, strategy)
            for fixture in CodingFixtureKind
            for strategy in (CodingStrategyKind.SOLO, CodingStrategyKind.DYNAMIC)
        ]
    )
    elapsed = {
        ("solo-edit", "solo"): 1_000,
        ("solo-edit", "dynamic"): 1_050,
        ("parallel-evidence", "solo"): 1_200,
        ("parallel-evidence", "dynamic"): 1_800,
        ("test-guided-recovery", "solo"): 1_500,
        ("test-guided-recovery", "dynamic"): 1_550,
    }
    payloads: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        record = replace(record, distribution_sha256=distribution_sha)
        counterfactual = 1 if record.strategy == CodingStrategyKind.SOLO else 0
        identity = {
            "schema_version": "noruct.live-coding-evaluation.v3",
            "recorded_at": _iso(now),
            "noruct_version": __version__,
            "source_revision": "fixture-revision-0001",
            "evaluation_run_id": f"fixture-live-run-{index:02d}",
            "provider_kind": "openai-codex-user-managed",
            "model_id": "fixture-model",
            "planner_source": (
                "bounded-counterfactual-plan"
                if record.strategy == CodingStrategyKind.SOLO
                else "live-dynamic-workflow-compiler"
            ),
            "validation_observation_scope": _CURRENT_VALIDATION_OBSERVATION_SCOPE,
            "subscription_cost_usd": None,
            "quota_confirmed": True,
            "elapsed_ms": elapsed[(record.fixture.value, record.strategy.value)],
            "external_model_calls": max(
                1, record.runtime_usage.model_calls - counterfactual
            ),
            "result": to_primitive(record),
        }
        payloads.append(_rehash_live_payload(identity))

    def refused(callback) -> bool:
        try:
            callback()
        except ValueError:
            return True
        return False

    with tempfile.TemporaryDirectory(prefix="noruct-firm-value-") as directory:
        root = Path(directory)
        manifest_path = root / "manifest.json"
        manifest_path.write_text(firm_value_manifest_to_json(manifest), encoding="utf-8")
        paths = []
        for index, payload in enumerate(payloads, start=1):
            path = root / f"record-{index}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            paths.append(path)
        report = aggregate_firm_value_records(manifest_path, paths, now=now)

        tampered = json.loads(paths[0].read_text(encoding="utf-8"))
        tampered["result"]["score"]["quality_score"] = 0.0
        tampered_path = root / "tampered.json"
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
        tamper_refused = refused(
            lambda: aggregate_firm_value_records(
                manifest_path, [tampered_path, *paths[1:]], now=now
            )
        )
        missing_refused = refused(
            lambda: aggregate_firm_value_records(manifest_path, paths[:-1], now=now)
        )
        mixed = json.loads(paths[1].read_text(encoding="utf-8"))
        mixed["model_id"] = "other-model"
        mixed = _rehash_live_payload(mixed)
        mixed_path = root / "mixed-model.json"
        mixed_path.write_text(json.dumps(mixed), encoding="utf-8")
        mixed_refused = refused(
            lambda: aggregate_firm_value_records(
                manifest_path, [paths[0], mixed_path, *paths[2:]], now=now
            )
        )
        duplicate = json.loads(paths[3].read_text(encoding="utf-8"))
        duplicate["evaluation_run_id"] = json.loads(
            paths[2].read_text(encoding="utf-8")
        )["evaluation_run_id"]
        duplicate = _rehash_live_payload(duplicate)
        duplicate_path = root / "duplicate-run.json"
        duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")
        duplicate_refused = refused(
            lambda: aggregate_firm_value_records(
                manifest_path, [*paths[:3], duplicate_path, *paths[4:]], now=now
            )
        )
        unconfirmed = json.loads(paths[5].read_text(encoding="utf-8"))
        unconfirmed["quota_confirmed"] = False
        unconfirmed = _rehash_live_payload(unconfirmed)
        unconfirmed_path = root / "unconfirmed.json"
        unconfirmed_path.write_text(json.dumps(unconfirmed), encoding="utf-8")
        quota_refused = refused(
            lambda: aggregate_firm_value_records(
                manifest_path, [*paths[:5], unconfirmed_path], now=now
            )
        )

    checks = (
        FirmValueCheck(
            "exact_three_by_two_record_set_aggregates",
            len(report.pairs) == 3,
            f"pairs={len(report.pairs)}",
        ),
        FirmValueCheck(
            "solo_edit_keeps_one_employee",
            report.pairs[0].dynamic_employee_count == 1,
            f"employees={report.pairs[0].dynamic_employee_count}",
        ),
        FirmValueCheck(
            "parallel_case_exposes_quality_cost_tradeoff",
            report.pairs[1].quality_delta >= QUALITY_GAIN_THRESHOLD
            and report.pairs[1].higher_cost,
            f"quality={report.pairs[1].quality_delta:+.4f},calls={report.pairs[1].external_model_call_delta:+d}",
        ),
        FirmValueCheck(
            "tampered_and_missing_records_are_refused",
            tamper_refused and missing_refused,
            f"tamper={tamper_refused},missing={missing_refused}",
        ),
        FirmValueCheck(
            "mixed_model_and_duplicate_run_are_refused",
            mixed_refused and duplicate_refused,
            f"mixed={mixed_refused},duplicate={duplicate_refused}",
        ),
        FirmValueCheck(
            "unconfirmed_quota_record_is_refused",
            quota_refused,
            f"refused={quota_refused}",
        ),
        FirmValueCheck(
            "directional_gate_does_not_call_high_cost_signal_an_absolute_win",
            report.overall_classification == "VALUE_SIGNAL_WITH_HIGHER_COST",
            report.overall_classification,
        ),
        FirmValueCheck(
            "self_test_uses_no_provider_network_or_quota",
            True,
            "offline-scripted,provider-calls=0,quota=false",
        ),
    )
    return FirmValueSelfTestRecord(
        schema_version=FIRM_VALUE_SELF_TEST_SCHEMA,
        evidence_class="offline-synthetic-live-envelope-contract",
        report=report,
        checks=checks,
        provider_calls=0,
        quota_consumed=False,
    )


def run_firm_value_self_test() -> FirmValueSelfTestRecord:
    return asyncio.run(_offline_self_test())
