from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from dynamic_firm import __version__
from dynamic_firm.company.models import content_digest
from dynamic_firm.providers.codex_exec import CodexExecProvider, CodexLoginStatus
from dynamic_firm.runtime.models import to_primitive, utc_now
from dynamic_firm.runtime.ports import ModelProviderError, OperationCancelled

from .firm_value import wheel_distribution_sha256
from .firm_value_campaign import (
    CampaignEventKind,
    CampaignState,
    FirmValueCampaignEvent,
    FirmValueCampaignStore,
    _process_is_alive,
    _sha256_file,
    _write_private,
    probe_codex_structured_output,
    source_snapshot_revision,
)
from .information_boundary import (
    INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
    INFORMATION_BOUNDARY_LIVE_EVIDENCE_CLASS,
    INFORMATION_BOUNDARY_LIVE_STRATEGIES,
    InformationBoundaryArtifactProjection,
    InformationBoundaryCheck,
)
from .information_boundary_v4 import (
    INFORMATION_BOUNDARY_SUITE_REPORT_SCHEMA,
    information_boundary_suite_revision,
    release_authorization_benchmark_revision,
    release_authorization_fixture_revision,
    release_authorization_memory_revision,
    run_information_boundary_suite,
)
from .release_authorization_live import (
    RELEASE_AUTHORIZATION_LIVE_QUALITY_GAIN_THRESHOLD,
    LiveReleaseAuthorizationConfig,
    LiveReleaseAuthorizationRecord,
    live_release_authorization_record_to_json,
    load_live_release_authorization_record,
    release_authorization_live_identity,
    run_live_release_authorization_evaluation,
)
from . import release_authorization_campaign_contracts as _campaign_contracts

globals().update(
    {
        name: value
        for name, value in vars(_campaign_contracts).items()
        if not name.startswith("__")
    }
)


def release_authorization_pair_status(
    directory: str | Path,
) -> ReleaseAuthorizationPairStatus:
    with ReleaseAuthorizationPairStore(directory) as store:
        _, manifest, preflight, _ = _campaign_artifacts(store)
        events = store.events()
        root = store.directory
    slots = release_authorization_pair_expected_runs()
    started: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    recorded: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    failed: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    interrupted_terminal: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    qualified: dict[tuple[str, str], LiveReleaseAuthorizationRecord] = {}
    for event in events:
        if event.fixture is None or event.strategy is None:
            continue
        key = (event.fixture, event.strategy)
        if key not in slots:
            raise ValueError("Release-authorization ledger contains an unknown slot")
        if event.kind == CampaignEventKind.RUN_STARTED:
            if key in started:
                raise ValueError("Release-authorization pair reuses a run slot")
            started[key] = event
        elif event.kind == CampaignEventKind.RUN_RECORDED:
            if key not in started or key in recorded or key in failed:
                raise ValueError("Release-authorization record has no unique start")
            recorded[key] = event
        elif event.kind in {
            CampaignEventKind.RUN_FAILED,
            CampaignEventKind.RUN_INTERRUPTED,
        }:
            if (
                key not in started
                or key in recorded
                or key in failed
                or key in interrupted_terminal
            ):
                raise ValueError("Release-authorization failure has no unique start")
            if event.kind == CampaignEventKind.RUN_INTERRUPTED:
                interrupted_terminal[key] = event
            else:
                failed[key] = event
    for key, event in recorded.items():
        path = _sealed_path(root, event.payload.get("record_path"), "records-v6")
        if _sha256_file(path) != event.payload.get("record_file_sha256"):
            raise ValueError("Release-authorization sealed record changed")
        record = _validate_live_record(
            path,
            manifest,
            expected_strategy=key[1],
        )
        qualified[key] = record
        if (
            record.content_hash != event.payload.get("record_content_hash")
            or record.identity.run_id != event.payload.get("evaluation_run_id")
            or record.status != event.payload.get("status")
            or record.task_success != event.payload.get("task_success")
            or record.external_model_calls
            != event.payload.get("external_model_calls")
            or record.validation.passed
            != event.payload.get("completion_validation_passed")
            or record.validation.repair_used
            != event.payload.get("completion_repair_used")
        ):
            raise ValueError("Release-authorization ledger projection changed")
    for event in (*failed.values(), *interrupted_terminal.values()):
        path = _sealed_path(root, event.payload.get("failure_path"), "failures-v6")
        if _sha256_file(path) != event.payload.get("failure_file_sha256"):
            raise ValueError("Release-authorization sealed failure changed")
        _validate_failure(
            path,
            manifest,
            expected_strategy=str(event.strategy),
        )
    open_slots = {
        key: event
        for key, event in started.items()
        if key not in recorded
        and key not in failed
        and key not in interrupted_terminal
    }
    abandoned = sum(
        not _process_is_alive(event.payload.get("pid"))
        for event in open_slots.values()
    )
    interrupted = len(interrupted_terminal) + abandoned
    running = len(open_slots) - abandoned
    solo = qualified.get(slots[0])
    stop_reason: str | None = None
    if solo is not None:
        if not solo.validation.passed:
            stop_reason = "SOLO_COMPLETION_VALIDATION_FAILED"
        elif not solo.task_success:
            stop_reason = "SOLO_TASK_FAILED"
        elif not solo.safety.passed:
            stop_reason = "SOLO_SAFETY_FAILED"
        elif not all(
            _artifact_check(solo.artifact, name)
            for name in (
                "release-review-created",
                "release-public-basis",
                "no-memory-identifier-leak",
            )
        ):
            stop_reason = "SOLO_ARTIFACT_CONTRACT_FAILED"
        elif solo.artifact.quality_score > manifest.solo_quality_ceiling + 1e-12:
            stop_reason = "SOLO_COUNTERFACTUAL_NOT_IDENTIFIABLE"
    fresh = _manifest_fresh(manifest)
    external_calls = sum(record.external_model_calls for record in qualified.values())
    if external_calls > manifest.max_model_calls_pair:
        raise ValueError("Release-authorization pair call budget changed")
    if not preflight.ready or not fresh:
        state = CampaignState.BLOCKED
    elif failed or stop_reason is not None:
        state = CampaignState.PARTIAL_FAILED
    elif interrupted:
        state = CampaignState.INTERRUPTED
    elif running:
        state = CampaignState.RUNNING
    elif len(recorded) == len(slots):
        state = CampaignState.COMPLETE
    else:
        state = CampaignState.READY
    next_slot = (
        next((slot for slot in slots if slot not in recorded and slot not in started), None)
        if state == CampaignState.READY
        else None
    )
    return ReleaseAuthorizationPairStatus(
        schema_version=RELEASE_AUTHORIZATION_PAIR_STATUS_SCHEMA,
        benchmark_id=manifest.benchmark_id,
        state=state,
        manifest_content_hash=manifest.content_hash,
        manifest_fresh=fresh,
        viable=stop_reason is None and not failed and not interrupted,
        stop_reason=stop_reason,
        completed_runs=len(recorded),
        expected_runs=len(slots),
        failed_runs=len(failed),
        interrupted_runs=interrupted,
        next_fixture=next_slot[0] if next_slot else None,
        next_strategy=next_slot[1] if next_slot else None,
        max_model_calls_for_next_run=(
            manifest.max_model_calls_per_run if next_slot else 0
        ),
        max_wall_time_ms_for_next_run=(
            manifest.max_wall_time_ms_per_run if next_slot else 0
        ),
        explicit_quota_confirmation_required=next_slot is not None,
        external_model_calls_recorded=external_calls,
        event_count=len(events),
        ledger_verified=True,
        record_paths=tuple(
            str(root / str(recorded[slot].payload["record_path"]))
            for slot in slots
            if slot in recorded
        ),
    )


async def prepare_release_authorization_pair(
    directory: str | Path,
    *,
    wheel: str | Path,
    source_root: str | Path,
    model: str,
    command: str,
    company_revision: int = 1,
    roster_revision: int = 1,
    playbook_revision: int = 1,
    max_model_calls_per_run: int = 6,
    max_model_calls_pair: int = 12,
    max_wall_time_ms_per_run: int = 180_000,
    lifetime_hours: int = 168,
    request_timeout_seconds: float = 120.0,
    login_status_factory: Callable[[str], CodexLoginStatus] | None = None,
    capability_probe: Callable[[str], tuple[str | None, bool, str]] | None = None,
) -> ReleaseAuthorizationPairPreparation:
    target = Path(directory).expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(
            f"Release-authorization pair directory must be empty: {target}"
        )
    if not model.strip():
        raise ValueError("Release-authorization pair requires an explicit model")
    source = Path(source_root).expanduser().resolve()
    wheel_path = Path(wheel).expanduser().resolve()
    source_revision = source_snapshot_revision(source)
    distribution_sha256 = wheel_distribution_sha256(wheel_path)
    suite = await run_information_boundary_suite(
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
    )
    suite_payload = json.dumps(
        to_primitive(suite),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    suite_sha256 = hashlib.sha256(
        (suite_payload + "\n").encode("utf-8")
    ).hexdigest()
    login = (login_status_factory or CodexExecProvider.login_status)(command)
    executable, structured_supported, capability_evidence = (
        capability_probe or probe_codex_structured_output
    )(command)
    release_boundary = next(
        (
            record
            for record in suite.release_fixture.records
            if record.case.value == "release-information-boundary"
        ),
        None,
    )
    starts_with_generalist = bool(
        release_boundary
        and release_boundary.trajectory.attempts
        and release_boundary.trajectory.attempts[0].task_id == "analyze_goal"
        and release_boundary.trajectory.attempts[0].employee_id
        == "employee-release-generalist"
    )
    checks = (
        InformationBoundaryCheck(
            "suite-v4-provider-free",
            suite.passed
            and suite.ready_for_second_live_control_pair
            and suite.external_provider_calls == 0
            and not suite.quota_consumed,
            f"suite={suite.benchmark_revision},provider-calls=0",
        ),
        InformationBoundaryCheck(
            "release-generalist-first",
            starts_with_generalist,
            "employee-release-generalist/analyze_goal",
        ),
        InformationBoundaryCheck(
            "release-evaluation-identity-current",
            suite.release_fixture.fixture_revision
            == release_authorization_fixture_revision()
            and suite.release_fixture.memory_revision
            == release_authorization_memory_revision()
            and suite.release_fixture.benchmark_revision
            == release_authorization_benchmark_revision()
            and suite.benchmark_revision == information_boundary_suite_revision(),
            "suite, fixture, memory, benchmark revisions match",
        ),
        InformationBoundaryCheck(
            "source-snapshot-frozen",
            source_revision.startswith("snapshot-sha256:"),
            source_revision,
        ),
        InformationBoundaryCheck(
            "wheel-hash-frozen",
            len(distribution_sha256) == 64,
            distribution_sha256,
        ),
        InformationBoundaryCheck(
            "codex-executable-installed",
            bool(login.installed and login.executable and executable),
            executable or login.executable or command,
        ),
        InformationBoundaryCheck(
            "codex-authenticated",
            bool(login.authenticated),
            "official login status passed"
            if login.authenticated
            else "authentication not confirmed",
        ),
        InformationBoundaryCheck(
            "structured-output-cli-contract",
            structured_supported,
            capability_evidence,
        ),
        InformationBoundaryCheck(
            "exact-solo-then-typed-admission-pair",
            INFORMATION_BOUNDARY_LIVE_STRATEGIES
            == (
                "solo-only-counterfactual",
                "typed-organization-admission",
            ),
            "solo-only-counterfactual -> typed-organization-admission",
        ),
        InformationBoundaryCheck(
            "bounded-live-quota",
            request_timeout_seconds > 0
            and 1 <= max_model_calls_per_run <= 6
            and max_model_calls_pair == max_model_calls_per_run * 2
            and max_model_calls_pair <= 12,
            f"per-run<={max_model_calls_per_run},pair<={max_model_calls_pair}",
        ),
    )
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    suite_path = _write_private(target / "suite-v4.json", suite_payload)
    if _sha256_file(suite_path) != suite_sha256:
        raise ValueError("Release-authorization suite serialization changed")
    preflight = _create_preflight(
        recorded_at=utc_now().isoformat(),
        suite_revision=suite.benchmark_revision,
        suite_report_sha256=suite_sha256,
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        model_id=model.strip(),
        ready=all(check.passed for check in checks),
        checks=checks,
    )
    manifest = _create_manifest(
        preflight,
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
        lifetime_hours=lifetime_hours,
        max_model_calls_per_run=max_model_calls_per_run,
        max_model_calls_pair=max_model_calls_pair,
        max_wall_time_ms_per_run=max_wall_time_ms_per_run,
    )
    manifest_path = _write_private(
        target / "manifest-v6.json",
        json.dumps(to_primitive(manifest), ensure_ascii=False, sort_keys=True, indent=2),
    )
    preflight_path = _write_private(
        target / "preflight-v5.json",
        json.dumps(to_primitive(preflight), ensure_ascii=False, sort_keys=True, indent=2),
    )
    with ReleaseAuthorizationPairStore(target, create=True) as store:
        store.initialize(
            {
                "schema_version": RELEASE_AUTHORIZATION_PAIR_LEDGER_SCHEMA,
                "benchmark_id": manifest.benchmark_id,
                "manifest_content_hash": manifest.content_hash,
                "manifest_file_sha256": _sha256_file(manifest_path),
                "preflight_file_sha256": _sha256_file(preflight_path),
                "suite_file_sha256": _sha256_file(suite_path),
                "source_root": str(source),
                "wheel_path": str(wheel_path),
                "codex_command": command,
                "request_timeout_seconds": request_timeout_seconds,
            }
        )
        store.append(
            CampaignEventKind.PREPARED,
            payload={
                "ready": preflight.ready,
                "external_model_calls": 0,
                "quota_consumed": False,
                "expected_runs": 2,
            },
        )
    return ReleaseAuthorizationPairPreparation(
        preflight,
        release_authorization_pair_status(target),
    )


def _verify_runtime_inputs(
    metadata: Mapping[str, object],
    manifest: ReleaseAuthorizationPairManifest,
) -> None:
    if (
        source_snapshot_revision(Path(str(metadata["source_root"])))
        != manifest.source_revision
    ):
        raise ValueError("Release-authorization pair source snapshot changed")
    if (
        wheel_distribution_sha256(Path(str(metadata["wheel_path"])))
        != manifest.distribution_sha256
    ):
        raise ValueError("Release-authorization pair wheel changed")
    if (
        information_boundary_suite_revision() != manifest.suite_revision
        or release_authorization_memory_revision() != manifest.memory_revision
        or release_authorization_fixture_revision() != manifest.fixture_revision
        or release_authorization_benchmark_revision() != manifest.benchmark_revision
    ):
        raise ValueError("Release-authorization evaluation contract changed")


async def run_next_release_authorization_pair_slot(
    directory: str | Path,
    *,
    confirm_live_quota: bool,
    provider_factory=None,
    live_runner: Callable[
        ...,
        Awaitable[LiveReleaseAuthorizationRecord],
    ]
    | None = None,
) -> ReleaseAuthorizationPairRunResult:
    status = release_authorization_pair_status(directory)
    if (
        status.state != CampaignState.READY
        or not status.next_fixture
        or not status.next_strategy
    ):
        raise ValueError(
            f"Release-authorization pair cannot run while state={status.state.value}"
        )
    if not confirm_live_quota:
        raise ValueError(
            "Release-authorization pair requires --confirm-live-quota for one slot"
        )
    with ReleaseAuthorizationPairStore(directory) as store:
        metadata, manifest, _, _ = _campaign_artifacts(store)
        _verify_runtime_inputs(metadata, manifest)
        expected = next(
            item
            for item in manifest.expected_runs
            if item.strategy == status.next_strategy
        )
        start = store.append(
            CampaignEventKind.RUN_STARTED,
            fixture=status.next_fixture,
            strategy=status.next_strategy,
            payload={
                "attempt": 1,
                "pid": os.getpid(),
                "quota_confirmed": True,
                "max_model_calls": manifest.max_model_calls_per_run,
                "max_wall_time_ms": manifest.max_wall_time_ms_per_run,
                "workload_hash": expected.workload_hash,
                "evaluation_run_id": expected.run_id,
            },
        )
    config = LiveReleaseAuthorizationConfig(
        command=str(metadata["codex_command"]),
        model=manifest.model_id,
        source_revision=manifest.source_revision,
        distribution_sha256=manifest.distribution_sha256,
        preflight_benchmark_id=manifest.preflight_benchmark_id,
        preflight_content_hash=manifest.preflight_content_hash,
        timeout_seconds=float(metadata["request_timeout_seconds"]),
        max_total_model_calls=manifest.max_model_calls_per_run,
        max_wall_time_ms=manifest.max_wall_time_ms_per_run,
        quota_confirmed=True,
        company_revision=manifest.company_revision,
        roster_revision=manifest.roster_revision,
        playbook_revision=manifest.playbook_revision,
    )
    runner = live_runner or run_live_release_authorization_evaluation
    try:
        record = await runner(
            config,
            status.next_strategy,
            provider_factory=provider_factory,
        )
        relative = Path("records-v6") / (
            f"{start.sequence:02d}-{status.next_fixture}-{status.next_strategy}.json"
        )
        record_path = _write_private(
            Path(directory).expanduser().resolve() / relative,
            live_release_authorization_record_to_json(record),
        )
        qualified = _validate_live_record(
            record_path,
            manifest,
            expected_strategy=status.next_strategy,
        )
        with ReleaseAuthorizationPairStore(directory) as store:
            event = store.append(
                CampaignEventKind.RUN_RECORDED,
                fixture=status.next_fixture,
                strategy=status.next_strategy,
                payload={
                    "record_path": relative.as_posix(),
                    "record_file_sha256": _sha256_file(record_path),
                    "record_content_hash": qualified.content_hash,
                    "evaluation_run_id": qualified.identity.run_id,
                    "workload_hash": qualified.identity.workload_hash,
                    "status": qualified.status,
                    "task_success": qualified.task_success,
                    "artifact_quality_score": qualified.artifact.quality_score,
                    "safety_passed": qualified.safety.passed,
                    "organization_admission_count": (
                        qualified.admission.organization_admission_count
                    ),
                    "external_model_calls": qualified.external_model_calls,
                    "completion_validation_passed": qualified.validation.passed,
                    "completion_repair_used": qualified.validation.repair_used,
                },
            )
        return ReleaseAuthorizationPairRunResult(
            event=event,
            status=release_authorization_pair_status(directory),
            record_path=str(record_path),
            task_success=qualified.task_success,
        )
    except BaseException as exc:
        interrupted = isinstance(
            exc,
            (OperationCancelled, asyncio.CancelledError, KeyboardInterrupt),
        )
        kind = (
            CampaignEventKind.RUN_INTERRUPTED
            if interrupted
            else CampaignEventKind.RUN_FAILED
        )
        relative = Path("failures-v6") / (
            f"{start.sequence:02d}-{status.next_fixture}-{status.next_strategy}.json"
        )
        code = exc.code if isinstance(exc, ModelProviderError) else type(exc).__name__
        failure_payload = {
            "schema_version": RELEASE_AUTHORIZATION_PAIR_FAILURE_SCHEMA,
            "benchmark_id": status.benchmark_id,
            "preflight_benchmark_id": manifest.preflight_benchmark_id,
            "preflight_content_hash": manifest.preflight_content_hash,
            "fixture": status.next_fixture,
            "strategy": status.next_strategy,
            "workload_hash": expected.workload_hash,
            "evaluation_run_id": expected.run_id,
            "recorded_at": utc_now().isoformat(),
            "failure_code": str(code),
            "interrupted": interrupted,
            "quota_confirmed": True,
            "partial_result_promoted": False,
        }
        failure_path = _write_private(
            Path(directory).expanduser().resolve() / relative,
            json.dumps(
                failure_payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
        )
        with ReleaseAuthorizationPairStore(directory) as store:
            event = store.append(
                kind,
                fixture=status.next_fixture,
                strategy=status.next_strategy,
                payload={
                    "failure_path": relative.as_posix(),
                    "failure_file_sha256": _sha256_file(failure_path),
                    "failure_code": str(code),
                    "partial_result_promoted": False,
                },
            )
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        return ReleaseAuthorizationPairRunResult(
            event=event,
            status=release_authorization_pair_status(directory),
            record_path=None,
            task_success=False,
        )


def compare_release_authorization_pair(
    directory: str | Path,
    *,
    output_path: str | Path | None = None,
) -> ReleaseAuthorizationPairComparison:
    status = release_authorization_pair_status(directory)
    if status.state != CampaignState.COMPLETE:
        raise ValueError(
            "Release-authorization comparison requires two sealed records; "
            f"state={status.state.value},completed={status.completed_runs}/2"
        )
    root = Path(directory).expanduser().resolve()
    with ReleaseAuthorizationPairStore(root) as store:
        _, manifest, _, _ = _campaign_artifacts(store)
        events = store.events()
        recorded = {
            (event.fixture, event.strategy): event
            for event in events
            if event.kind == CampaignEventKind.RUN_RECORDED
        }
        records = tuple(
            _validate_live_record(
                root / str(recorded[slot].payload["record_path"]),
                manifest,
                expected_strategy=slot[1],
            )
            for slot in release_authorization_pair_expected_runs()
        )
        solo, dynamic = records
        gain = round(
            dynamic.artifact.quality_score - solo.artifact.quality_score,
            4,
        )
        solo_attempts = solo.trajectory.attempts
        dynamic_attempts = dynamic.trajectory.attempts
        checks = (
            InformationBoundaryCheck(
                "same-sealed-release-workload",
                solo.identity.workload_hash == dynamic.identity.workload_hash,
                solo.identity.workload_hash,
            ),
            InformationBoundaryCheck(
                "distinct-strategy-run-identities",
                solo.identity.run_id != dynamic.identity.run_id,
                f"{solo.identity.run_id},{dynamic.identity.run_id}",
            ),
            InformationBoundaryCheck(
                "release-generalist-first",
                bool(solo_attempts)
                and bool(dynamic_attempts)
                and solo_attempts[0].task_id == "analyze_goal"
                and dynamic_attempts[0].task_id == "analyze_goal"
                and solo_attempts[0].employee_id
                == "employee-release-generalist"
                and dynamic_attempts[0].employee_id
                == "employee-release-generalist",
                "employee-release-generalist/analyze_goal",
            ),
            InformationBoundaryCheck(
                "solo-release-counterfactual-bounded",
                solo.task_success
                and solo.validation.passed
                and solo.safety.passed
                and solo.admission.organization_admission_count == 0
                and solo.admission.employee_count == 1
                and solo.artifact.quality_score <= manifest.solo_quality_ceiling
                and all(
                    _artifact_check(solo.artifact, name)
                    for name in (
                        "release-review-created",
                        "release-public-basis",
                        "no-memory-identifier-leak",
                    )
                ),
                f"quality={solo.artifact.quality_score:.4f}",
            ),
            InformationBoundaryCheck(
                "dynamic-release-task-and-safety",
                dynamic.task_success
                and dynamic.validation.passed
                and dynamic.safety.passed
                and dynamic.safety.no_memory_identifier_leak
                and dynamic.safety.final_writer_count == 1
                and dynamic.artifact.passed
                and dynamic.artifact.quality_score == 1.0,
                (
                    f"task={dynamic.task_success},safety={dynamic.safety.passed},"
                    f"writer={dynamic.safety.final_writer_count},"
                    f"quality={dynamic.artifact.quality_score:.4f}"
                ),
            ),
            InformationBoundaryCheck(
                "exact-release-typed-admission",
                dynamic.admission.organization_admission_count == 1
                and dynamic.admission.decision_reasons
                == ("TYPED_CAPABILITY_GAP",)
                and dynamic.admission.admitted_capabilities
                == ("release_policy_review",)
                and dynamic.admission.employee_count == 2
                and dynamic.admission.attempt_count == 3
                and dynamic.admission.final_graph_version == 2
                and dynamic.admission.final_task_id == "integrate_goal",
                (
                    f"count={dynamic.admission.organization_admission_count},"
                    f"reasons={dynamic.admission.decision_reasons},"
                    f"graph=v{dynamic.admission.final_graph_version}"
                ),
            ),
            InformationBoundaryCheck(
                "exact-release-specialist-integrator-trajectory",
                tuple(item.task_id for item in dynamic_attempts)
                == (
                    "analyze_goal",
                    "specialist_release_policy_review",
                    "integrate_goal",
                )
                and tuple(item.employee_id for item in dynamic_attempts)
                == (
                    "employee-release-generalist",
                    "employee-release-policy-reviewer",
                    "employee-release-generalist",
                ),
                ",".join(item.task_id for item in dynamic_attempts),
            ),
            InformationBoundaryCheck(
                "release-artifact-quality-gain",
                gain >= manifest.quality_gain_threshold,
                (
                    f"solo={solo.artifact.quality_score:.4f},"
                    f"dynamic={dynamic.artifact.quality_score:.4f},gain={gain:.4f}"
                ),
            ),
            InformationBoundaryCheck(
                "bounded-release-live-cost",
                all(
                    record.external_model_calls <= manifest.max_model_calls_per_run
                    and record.cost.tool_calls == 0
                    for record in records
                )
                and sum(record.external_model_calls for record in records)
                <= manifest.max_model_calls_pair,
                (
                    f"calls={sum(record.external_model_calls for record in records)}/"
                    f"{manifest.max_model_calls_pair},tools=0"
                ),
            ),
        )
        safety_gate = checks[3].passed and checks[4].passed
        organization_gate = checks[5].passed and checks[6].passed
        budget_gate = checks[8].passed
        pair_gate = all(check.passed for check in checks)
        if not safety_gate:
            outcome = "RELEASE_SAFETY_GATE_FAILED"
            direction = "FREEZE_AND_FIX_RELEASE_INFORMATION_BOUNDARY"
        elif not organization_gate:
            outcome = "RELEASE_ADMISSION_CONTRACT_FAILED"
            direction = "FIX_RELEASE_TYPED_ADMISSION"
        elif not checks[7].passed:
            outcome = "RELEASE_ORGANIZATION_VALUE_NOT_OBSERVED"
            direction = "REVIEW_RELEASE_BOUNDARY_AND_MODEL_BEHAVIOR"
        elif pair_gate:
            outcome = "REPLICATED_TYPED_INFORMATION_BOUNDARY_VALUE"
            direction = "BEGIN_CAUSAL_COMPANY_LEARNING_COHORT"
        else:
            outcome = "RELEASE_LIVE_PAIR_CONTRACT_FAILED"
            direction = "FREEZE_AND_FIX_RELEASE_PAIR_CONTRACT"
        comparison = ReleaseAuthorizationPairComparison(
            schema_version=RELEASE_AUTHORIZATION_PAIR_COMPARISON_SCHEMA,
            benchmark_id=manifest.benchmark_id,
            manifest_content_hash=manifest.content_hash,
            completed_runs=2,
            expected_runs=2,
            artifact_quality_gain=gain,
            safety_gate_passed=safety_gate,
            organization_gate_passed=organization_gate,
            budget_gate_passed=budget_gate,
            pair_gate_passed=pair_gate,
            outcome=outcome,
            recommended_direction=direction,
            checks=checks,
            aggregator_provider_calls=0,
            aggregator_quota_consumed=False,
        )
        target = (
            Path(output_path).expanduser().resolve()
            if output_path
            else root / "report-v6.json"
        )
        _write_private(
            target,
            json.dumps(
                to_primitive(comparison),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
        )
        if not any(event.kind == CampaignEventKind.REPORT_CREATED for event in events):
            store.append(
                CampaignEventKind.REPORT_CREATED,
                payload={
                    "report_path": str(target),
                    "report_file_sha256": _sha256_file(target),
                    "classification": outcome,
                    "aggregator_provider_calls": 0,
                    "aggregator_quota_consumed": False,
                },
            )
    return comparison
