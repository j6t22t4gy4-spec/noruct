"""Sealed 4-way Manager-value campaign control plane.

This module intentionally seals *already executed* arm evidence rather than
pretending a pre-existing SOLO/DYNAMIC runner is a Manager runner.  A concrete
arm executor may create a candidate record, but this module owns immutable
slot identity, source/model/budget comparability, explicit per-slot consent,
and append-only campaign evidence.
"""

from __future__ import annotations

import json
import os
import asyncio
import math
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from dynamic_firm.company.models import content_digest
from dynamic_firm.runtime.models import to_primitive, utc_now

from .firm_value import wheel_distribution_sha256
from .firm_value_campaign import (
    CampaignEventKind,
    CampaignState,
    FirmValueCampaignEvent,
    FirmValueCampaignStore,
    _sha256_file,
    _write_private,
    source_snapshot_revision,
)
from .manager_value_contract import ManagerValueArm, manager_value_qualification_contract
from .manager_value_campaign_manifest import (
    LEGACY_MANAGER_CAMPAIGN_SCHEMAS as _LEGACY_MANAGER_CAMPAIGN_SCHEMAS,
    MANAGER_CAMPAIGN_SCHEMA,
    ManagerValueCampaignManifest,
)


MANAGER_CAMPAIGN_STATUS_SCHEMA = "noruct.manager-value-campaign-status.v1"
MANAGER_CAMPAIGN_PREFLIGHT_SCHEMA = "noruct.manager-value-campaign-preflight.v1"
MANAGER_CAMPAIGN_RECORD_SCHEMA = "noruct.manager-value-live-record.v8"
_LEGACY_MANAGER_CAMPAIGN_RECORD_SCHEMAS = frozenset(
    {
        "noruct.manager-value-live-record.v3",
        "noruct.manager-value-live-record.v4",
        "noruct.manager-value-live-record.v5",
        "noruct.manager-value-live-record.v6",
    }
)
_PRE_METRICS_MANAGER_CAMPAIGN_RECORD_SCHEMAS = frozenset(
    {
        *_LEGACY_MANAGER_CAMPAIGN_RECORD_SCHEMAS,
        "noruct.manager-value-live-record.v7",
    }
)
_DB_NAME = "manager-value-campaign.db"


@dataclass(frozen=True, slots=True)
class ManagerValueLiveRecord:
    schema_version: str
    record_id: str
    content_hash: str
    recorded_at: str
    fixture: str
    fixture_revision: str
    arm: str
    source_revision: str
    distribution_sha256: str
    model_id: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    configured_model_call_limit: int
    configured_wall_time_ms: int
    quota_confirmed: bool
    evaluator_risk_confirmed: bool
    task_success: bool
    safety_passed: bool
    quality_score: float
    external_model_calls: int
    elapsed_ms: int
    employee_count: int
    capability_profile_count: int
    manager_bound: bool
    # A same-profile execution replica is intentionally not counted as a new
    # persistent employee.  These fields prove actual bounded fan-out without
    # corrupting the homogeneous-vs-heterogeneous counterfactual.
    execution_replica_count: int = 0
    replica_group_count: int = 0
    # Only the Manager-led arm may carry these provenance facts. They prove
    # that the compiler received the persistent Manager's immutable planning
    # assignment and bounded brief before it accepted the staffing proposal.
    manager_planning_owner_id: str = ""
    manager_planning_assignment_digest: str = ""
    manager_planning_brief_digest: str = ""
    # A live Manager slot must exercise the Manager-bound Compiler rather than
    # merely replaying a fixed counterfactual graph with Manager metadata.
    compiler_planning_exercised: bool = False
    # v5 makes a failed live arm attributable without retaining prompts,
    # transcript, workspace content, tool arguments or employee output.
    planning_mode: str = ""
    planning_reason: str = ""
    failure_reason_safe: str = ""
    employee_failure_codes: tuple[str, ...] = ()
    task_attempt_count: int = 0
    successful_task_attempt_count: int = 0
    # v6 completes the frozen comparison envelope: user approval friction is
    # an outcome, not a hidden side effect of one arm's tool sequence.
    approvals_requested: int = 0
    approvals_granted: int = 0
    # Subscription-backed commands often cannot report a truthful USD price.
    # Keep zero-dollar usage from becoming a false cost claim: in that case
    # the frozen same-model call count is the only cost comparison proxy.
    reported_cost_usd: float = 0.0
    cost_accounting_mode: str = "MODEL_CALL_PROXY"
    # v8 measures the tail costs that averages previously hid. Counts are
    # privacy-bounded and come from validation receipts, the Job operator
    # signal ledger, and durable external-effect action status respectively.
    validation_attempt_count: int = 0
    validation_recovery_attempt_count: int = 0
    validation_recovery_success_count: int = 0
    runtime_user_intervention_count: int = 0
    external_effect_error_count: int = 0
    external_effect_unknown_count: int = 0
    intervention_accounting_mode: str = "RUNTIME_OPERATOR_SIGNAL_LEDGER"
    external_effect_accounting_mode: str = "DURABLE_TOOL_ACTION_STATUS"

    def content_payload(self) -> Mapping[str, object]:
        payload = {
            key: value
            for key, value in to_primitive(self).items()
            if key not in {"record_id", "content_hash"}
        }
        # v3 predates replica and failure-attribution fields. v4 already
        # recorded replica shape, so removing it would rewrite its historical
        # content hash. Keep legacy hashes verifiable while requiring v6
        # diagnostics and approval-friction fields for new campaign records.
        if self.schema_version == "noruct.manager-value-live-record.v3":
            payload.pop("execution_replica_count", None)
            payload.pop("replica_group_count", None)
        if self.schema_version in _LEGACY_MANAGER_CAMPAIGN_RECORD_SCHEMAS:
            payload.pop("planning_mode", None)
            payload.pop("planning_reason", None)
            payload.pop("failure_reason_safe", None)
            payload.pop("employee_failure_codes", None)
            payload.pop("task_attempt_count", None)
            payload.pop("successful_task_attempt_count", None)
            payload.pop("approvals_requested", None)
            payload.pop("approvals_granted", None)
            payload.pop("reported_cost_usd", None)
            payload.pop("cost_accounting_mode", None)
        if self.schema_version in _PRE_METRICS_MANAGER_CAMPAIGN_RECORD_SCHEMAS:
            payload.pop("validation_attempt_count", None)
            payload.pop("validation_recovery_attempt_count", None)
            payload.pop("validation_recovery_success_count", None)
            payload.pop("runtime_user_intervention_count", None)
            payload.pop("external_effect_error_count", None)
            payload.pop("external_effect_unknown_count", None)
            payload.pop("intervention_accounting_mode", None)
            payload.pop("external_effect_accounting_mode", None)
        return payload


@dataclass(frozen=True, slots=True)
class ManagerValueCampaignStatus:
    schema_version: str
    benchmark_id: str
    state: CampaignState
    completed_runs: int
    expected_runs: int
    next_fixture: str | None
    next_arm: str | None
    explicit_quota_confirmation_required: bool
    explicit_evaluator_risk_confirmation_required: bool
    ledger_verified: bool
    record_paths: tuple[str, ...]
    failed_runs: int = 0
    interrupted_runs: int = 0
    running_runs: int = 0
    external_model_calls_recorded: int = 0
    # Calls from a failed or interrupted process are not observable safely.
    # Their frozen slot envelope is therefore forfeited rather than reported
    # as zero.  Keep this separate from observed successful-record usage so a
    # campaign report never mistakes a reservation for a measured result.
    external_model_calls_forfeited: int = 0
    external_model_calls_accounted: int = 0
    event_count: int = 0


@dataclass(frozen=True, slots=True)
class ManagerValueCampaignCheck:
    """One non-effectful readiness fact for the next campaign slot."""

    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class ManagerValueCampaignPreflight:
    """Read-only readiness projection; it never consumes a campaign slot."""

    schema_version: str
    benchmark_id: str
    recorded_at: str
    state: CampaignState
    next_fixture: str | None
    next_arm: str | None
    model_id: str
    external_model_calls: int
    quota_consumed: bool
    ready: bool
    checks: tuple[ManagerValueCampaignCheck, ...]


@dataclass(frozen=True, slots=True)
class ManagerValueArmOutcome:
    arm: str
    run_count: int
    complete_failure_rate: float
    safety_failure_rate: float
    lower_decile_quality: float
    mean_quality: float
    mean_model_calls: float
    mean_elapsed_ms: float
    mean_approvals_requested: float
    mean_approvals_granted: float
    mean_reported_cost_usd: float | None
    cost_accounting_mode: str
    mean_validation_recovery_attempts: float = 0.0
    validation_recovery_success_rate: float | None = None
    mean_runtime_user_interventions: float = 0.0
    external_effect_error_rate: float = 0.0
    external_effect_unknown_rate: float = 0.0


@dataclass(frozen=True, slots=True)
class ManagerValueCampaignReport:
    schema_version: str
    benchmark_id: str
    content_hash: str
    created_at: str
    qualified: bool
    outcomes: tuple[ManagerValueArmOutcome, ...]
    manager_incremental_quality_vs_heterogeneous: float
    manager_incremental_model_calls_vs_heterogeneous: float
    outcome_claimed: bool = False

    def content_payload(self) -> Mapping[str, object]:
        return {key: value for key, value in to_primitive(self).items() if key != "content_hash"}


class ManagerValueCampaignStore(FirmValueCampaignStore):
    def __init__(
        self,
        directory: str | Path,
        *,
        create: bool = False,
        ledger_schema: str = MANAGER_CAMPAIGN_SCHEMA,
    ) -> None:
        super().__init__(directory, create=create, db_name=_DB_NAME, ledger_schema=ledger_schema, event_id_prefix="manager-value-event")


def _manifest_path(store: ManagerValueCampaignStore) -> Path:
    return store.directory / "manager-value-manifest.json"


def _create_manifest(*, source_revision: str, distribution_sha256: str, model_id: str, company_revision: int, roster_revision: int, playbook_revision: int, max_total_model_calls: int, max_wall_time_ms: int) -> ManagerValueCampaignManifest:
    if not source_revision.startswith("snapshot-sha256:") or len(distribution_sha256) != 64 or not model_id.strip():
        raise ValueError("Manager-value campaign requires frozen source, wheel, and explicit model")
    if not 4 <= max_total_model_calls <= 12 or not 1_000 <= max_wall_time_ms <= 600_000:
        raise ValueError("Manager-value campaign bounds are invalid")
    contract = manager_value_qualification_contract()
    fixture_revisions = tuple(
        (fixture.fixture, fixture.fixture_revision) for fixture in contract.fixtures
    )
    base = ManagerValueCampaignManifest(
        MANAGER_CAMPAIGN_SCHEMA, "pending", "pending", utc_now().isoformat(), source_revision,
        distribution_sha256, model_id.strip(), company_revision, roster_revision, playbook_revision,
        max_total_model_calls, max_wall_time_ms, contract.exact_slots, fixture_revisions,
    )
    digest = content_digest(base.content_payload())
    return ManagerValueCampaignManifest(**{**to_primitive(base), "benchmark_id": f"manager-value-{digest[:24]}", "content_hash": digest, "slots": contract.exact_slots})


def _load_manifest(path: Path) -> ManagerValueCampaignManifest:
    value = json.loads(path.read_text(encoding="utf-8"))
    schema_version = value.get("schema_version")
    if schema_version not in {MANAGER_CAMPAIGN_SCHEMA, *_LEGACY_MANAGER_CAMPAIGN_SCHEMAS}:
        raise ValueError("Manager-value campaign manifest schema is invalid")
    manifest = ManagerValueCampaignManifest(
        **{
            **value,
            "slots": tuple(tuple(item) for item in value["slots"]),
            "fixture_revisions": tuple(
                tuple(item) for item in value.get("fixture_revisions", ())
            ),
        }
    )
    contract = manager_value_qualification_contract()
    fixture_names = tuple(fixture.fixture for fixture in contract.fixtures)
    if (
        manifest.content_hash != content_digest(manifest.content_payload())
        or manifest.benchmark_id != f"manager-value-{manifest.content_hash[:24]}"
        or manifest.slots != contract.exact_slots
        or (
            manifest.schema_version == MANAGER_CAMPAIGN_SCHEMA
            and (
                tuple(name for name, _revision in manifest.fixture_revisions)
                != fixture_names
                or any(not revision for _name, revision in manifest.fixture_revisions)
            )
        )
        or (
            manifest.schema_version in _LEGACY_MANAGER_CAMPAIGN_SCHEMAS
            and manifest.fixture_revisions
        )
    ):
        raise ValueError("Manager-value campaign manifest contract is invalid")
    return manifest


def prepare_manager_value_campaign(directory: str | Path, *, wheel: str | Path, source_root: str | Path, model_id: str, company_revision: int = 0, roster_revision: int = 0, playbook_revision: int = 0, max_total_model_calls: int = 6, max_wall_time_ms: int = 180_000, codex_command: str = "codex", request_timeout_seconds: float = 120.0) -> ManagerValueCampaignStatus:
    target = Path(directory).expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError("Manager-value campaign directory must be empty")
    if not codex_command.strip() or request_timeout_seconds <= 0:
        raise ValueError("Manager-value campaign command and request timeout are invalid")
    manifest = _create_manifest(source_revision=source_snapshot_revision(source_root), distribution_sha256=wheel_distribution_sha256(Path(wheel)), model_id=model_id, company_revision=company_revision, roster_revision=roster_revision, playbook_revision=playbook_revision, max_total_model_calls=max_total_model_calls, max_wall_time_ms=max_wall_time_ms)
    target.mkdir(parents=True, exist_ok=True)
    manifest_path = _write_private(target / "manager-value-manifest.json", json.dumps(to_primitive(manifest), ensure_ascii=False, sort_keys=True, indent=2))
    with ManagerValueCampaignStore(target, create=True) as store:
        store.initialize({"manifest_sha256": _sha256_file(manifest_path), "benchmark_id": manifest.benchmark_id, "source_root": str(Path(source_root).resolve()), "wheel": str(Path(wheel).resolve()), "codex_command": codex_command.strip(), "request_timeout_seconds": request_timeout_seconds})
        store.append(CampaignEventKind.PREPARED, payload={"slots": len(manifest.slots), "external_model_calls": 0, "quota_consumed": False, "per_slot_confirmation": True})
    return manager_value_campaign_status(target)


def _artifacts(directory: str | Path) -> tuple[ManagerValueCampaignStore, ManagerValueCampaignManifest]:
    target = Path(directory).expanduser().resolve()
    path = target / "manager-value-manifest.json"
    manifest = _load_manifest(path)
    store = ManagerValueCampaignStore(target, ledger_schema=manifest.schema_version)
    metadata = store.metadata()
    if _sha256_file(path) != metadata.get("manifest_sha256"):
        store.close(); raise ValueError("Manager-value campaign manifest changed")
    return store, manifest


def _verify_runtime_inputs(store: ManagerValueCampaignStore, manifest: ManagerValueCampaignManifest) -> None:
    metadata = store.metadata()
    if source_snapshot_revision(Path(str(metadata["source_root"]))) != manifest.source_revision:
        raise ValueError("Manager-value campaign source snapshot changed after preparation")
    if wheel_distribution_sha256(Path(str(metadata["wheel"]))) != manifest.distribution_sha256:
        raise ValueError("Manager-value campaign wheel changed after preparation")


def _process_is_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        return False
    return True


def _command_available(command: str) -> tuple[bool, str]:
    """Check only executable availability; login is checked by the real provider.

    This intentionally does not run the command or query a provider.  A
    campaign preflight must remain a zero-quota, no-side-effect operation.
    """

    try:
        parts = shlex.split(command)
    except ValueError:
        return False, "invalid command quoting"
    if not parts:
        return False, "empty command"
    executable = parts[0]
    if "/" in executable or "\\" in executable:
        path = Path(executable).expanduser()
        return bool(path.is_file() and os.access(path, os.X_OK)), str(path)
    resolved = shutil.which(executable)
    return resolved is not None, resolved or executable


def manager_value_campaign_status(directory: str | Path) -> ManagerValueCampaignStatus:
    store, manifest = _artifacts(directory)
    try:
        events = store.events()
        prepared = [event for event in events if event.kind == CampaignEventKind.PREPARED]
        if len(prepared) != 1 or prepared[0].fixture is not None or prepared[0].strategy is not None:
            raise ValueError("Manager-value campaign must contain exactly one preparation event")
        started: dict[tuple[str, str], FirmValueCampaignEvent] = {}
        recorded: dict[tuple[str, str], FirmValueCampaignEvent] = {}
        failed: dict[tuple[str, str], FirmValueCampaignEvent] = {}
        terminal_interrupted: dict[tuple[str, str], FirmValueCampaignEvent] = {}
        for event in events:
            if event.kind == CampaignEventKind.PREPARED:
                continue
            if event.fixture is None or event.strategy is None:
                raise ValueError("Manager-value campaign event has no slot identity")
            key = (event.fixture, event.strategy)
            if key not in manifest.slots:
                raise ValueError("Manager-value campaign contains an unknown slot")
            if event.kind == CampaignEventKind.RUN_STARTED:
                if key in started:
                    raise ValueError("Manager-value campaign reuses a slot")
                started[key] = event
            elif event.kind == CampaignEventKind.RUN_RECORDED:
                if key not in started or key in recorded or key in failed or key in terminal_interrupted:
                    raise ValueError("Manager-value record has no unique started slot")
                recorded[key] = event
            elif event.kind in {CampaignEventKind.RUN_FAILED, CampaignEventKind.RUN_INTERRUPTED}:
                if key not in started or key in recorded or key in failed or key in terminal_interrupted:
                    raise ValueError("Manager-value failure has no unique started slot")
                failure_path = store.directory / str(event.payload.get("failure_path", ""))
                if not failure_path.is_file() or _sha256_file(failure_path) != event.payload.get("failure_file_sha256"):
                    raise ValueError("Manager-value failure receipt changed")
                reserved_calls = event.payload.get(
                    "reserved_external_model_calls", manifest.max_total_model_calls
                )
                if reserved_calls != manifest.max_total_model_calls:
                    raise ValueError("Manager-value failure reservation changed")
                if event.kind == CampaignEventKind.RUN_FAILED:
                    failed[key] = event
                else:
                    terminal_interrupted[key] = event
            else:
                raise ValueError("Manager-value campaign contains an unsupported event")
        for event in recorded.values():
            path = store.directory / str(event.payload["record_path"])
            if not path.is_file() or _sha256_file(path) != event.payload.get("record_file_sha256"):
                raise ValueError("Manager-value sealed record changed")
            _validate_record(_load_record(path), manifest, event.fixture or "", event.strategy or "")
        open_slots = {
            key: event
            for key, event in started.items()
            if key not in recorded and key not in failed and key not in terminal_interrupted
        }
        abandoned = sum(not _process_is_alive(event.payload.get("pid")) for event in open_slots.values())
        running = len(open_slots) - abandoned
        interrupted_count = len(terminal_interrupted) + abandoned
        # A process can die after reserving its one-time slot and before the
        # evidence file is sealed.  The campaign must not silently reuse that
        # slot, because its provider usage is unknown.
        next_slot = next((slot for slot in manifest.slots if slot not in started), None)
        state = (
            CampaignState.PARTIAL_FAILED if failed
            else CampaignState.INTERRUPTED if interrupted_count
            else CampaignState.RUNNING if running
            else CampaignState.COMPLETE if len(recorded) == len(manifest.slots)
            else CampaignState.READY
        )
        if state != CampaignState.READY:
            next_slot = None
        recorded_calls = sum(
            int(event.payload.get("external_model_calls", 0))
            for event in recorded.values()
        )
        # A terminal failed/interrupted slot, or a dead process that left only
        # RUN_STARTED, may already have reached the provider.  Unknown usage
        # must never reopen quota.  Use the exact frozen per-slot envelope;
        # legacy receipts without the field are interpreted conservatively.
        forfeited_calls = (
            len(failed) + len(terminal_interrupted) + abandoned
        ) * manifest.max_total_model_calls
        return ManagerValueCampaignStatus(
            MANAGER_CAMPAIGN_STATUS_SCHEMA,
            manifest.benchmark_id,
            state,
            len(recorded),
            len(manifest.slots),
            next_slot[0] if next_slot else None,
            next_slot[1] if next_slot else None,
            next_slot is not None,
            next_slot is not None,
            True,
            tuple(
                str(store.directory / str(recorded[slot].payload["record_path"]))
                for slot in manifest.slots
                if slot in recorded
            ),
            failed_runs=len(failed),
            interrupted_runs=interrupted_count,
            running_runs=running,
            external_model_calls_recorded=recorded_calls,
            external_model_calls_forfeited=forfeited_calls,
            external_model_calls_accounted=recorded_calls + forfeited_calls,
            event_count=len(events),
        )
    finally:
        store.close()


def preflight_manager_value_campaign(directory: str | Path) -> ManagerValueCampaignPreflight:
    """Verify the frozen execution envelope without creating a provider call.

    The next slot remains blocked until the user passes the two per-slot
    confirmations to ``run-next``.  Preflight only makes the reason visible.
    """

    status = manager_value_campaign_status(directory)
    store, manifest = _artifacts(directory)
    try:
        metadata = store.metadata()
        try:
            source_matches = (
                source_snapshot_revision(Path(str(metadata["source_root"])))
                == manifest.source_revision
            )
            source_evidence = manifest.source_revision
        except (OSError, ValueError) as exc:
            source_matches = False
            source_evidence = type(exc).__name__
        try:
            wheel_matches = (
                wheel_distribution_sha256(Path(str(metadata["wheel"])))
                == manifest.distribution_sha256
            )
            wheel_evidence = manifest.distribution_sha256
        except (OSError, ValueError) as exc:
            wheel_matches = False
            wheel_evidence = type(exc).__name__
        command_available, command_evidence = _command_available(
            str(metadata["codex_command"])
        )
        checks = (
            ManagerValueCampaignCheck("append-only-ledger", status.ledger_verified, f"events={status.event_count}"),
            ManagerValueCampaignCheck("campaign-slot-ready", status.state is CampaignState.READY, status.state.value),
            ManagerValueCampaignCheck("source-snapshot-frozen", source_matches, source_evidence),
            ManagerValueCampaignCheck("wheel-hash-frozen", wheel_matches, wheel_evidence),
            ManagerValueCampaignCheck("execution-command-available", command_available, command_evidence),
            ManagerValueCampaignCheck(
                "one-slot-bounded-envelope",
                4 <= manifest.max_total_model_calls <= 12
                and 1_000 <= manifest.max_wall_time_ms <= 600_000
                and float(metadata["request_timeout_seconds"]) > 0,
                f"calls<={manifest.max_total_model_calls},wall_ms<={manifest.max_wall_time_ms}",
            ),
            ManagerValueCampaignCheck(
                "per-slot-human-confirmation",
                True,
                "required by run-next; not consumed by preflight",
            ),
        )
        return ManagerValueCampaignPreflight(
            schema_version=MANAGER_CAMPAIGN_PREFLIGHT_SCHEMA,
            benchmark_id=manifest.benchmark_id,
            recorded_at=utc_now().isoformat(),
            state=status.state,
            next_fixture=status.next_fixture,
            next_arm=status.next_arm,
            model_id=manifest.model_id,
            external_model_calls=status.external_model_calls_accounted,
            quota_consumed=status.external_model_calls_accounted > 0,
            ready=all(check.passed for check in checks),
            checks=checks,
        )
    finally:
        store.close()


def _load_record(path: Path) -> ManagerValueLiveRecord:
    value = json.loads(path.read_text(encoding="utf-8"))
    record = ManagerValueLiveRecord(**value)
    if (
        record.schema_version
        not in {
            MANAGER_CAMPAIGN_RECORD_SCHEMA,
            *_PRE_METRICS_MANAGER_CAMPAIGN_RECORD_SCHEMAS,
        }
        or record.content_hash != content_digest(record.content_payload())
        or record.record_id != f"manager-value-live-{record.content_hash[:24]}"
    ):
        raise ValueError("Manager-value live record identity is invalid")
    return record


def _validate_record(record: ManagerValueLiveRecord, manifest: ManagerValueCampaignManifest, fixture: str, arm: str) -> None:
    expected_revision = manifest.fixture_revision_for(fixture)
    if (
        record.fixture != fixture
        or record.arm != arm
        or not record.fixture_revision
        or (
            expected_revision is not None
            and record.fixture_revision != expected_revision
        )
    ):
        raise ValueError("Manager-value live record slot does not match manifest")
    if (record.source_revision, record.distribution_sha256, record.model_id, record.company_revision, record.roster_revision, record.playbook_revision, record.configured_model_call_limit, record.configured_wall_time_ms) != (manifest.source_revision, manifest.distribution_sha256, manifest.model_id, manifest.company_revision, manifest.roster_revision, manifest.playbook_revision, manifest.max_total_model_calls, manifest.max_wall_time_ms):
        raise ValueError("Manager-value live record comparability envelope changed")
    if not record.quota_confirmed or not record.evaluator_risk_confirmed or record.external_model_calls > manifest.max_total_model_calls or record.elapsed_ms > manifest.max_wall_time_ms:
        raise ValueError("Manager-value live record violates bounded live consent")
    # A Manager-led Firm does not have to manufacture a team for a bounded
    # one-Employee task, nor does a safe Manager fallback have to dispatch an
    # Employee at all. Its differentiator is exercised, attributable Manager
    # planning and supervision under the same authority envelope. Requiring a
    # completed employee attempt would erase the refusal/fallback outcomes the
    # four-way campaign is meant to compare.
    shape = {ManagerValueArm.SINGLE_EMPLOYEE.value: (1, 1, False), ManagerValueArm.HOMOGENEOUS_GRAPH.value: (1, 1, False), ManagerValueArm.HETEROGENEOUS_GRAPH.value: (2, 2, False), ManagerValueArm.MANAGER_LED_FIRM.value: (0, 0, True)}[arm]
    if record.employee_count < shape[0] or record.capability_profile_count < shape[1] or record.manager_bound is not shape[2]:
        raise ValueError("Manager-value live record does not prove its arm runtime shape")
    if arm == ManagerValueArm.HOMOGENEOUS_GRAPH.value:
        if record.execution_replica_count < 2 or record.replica_group_count < 1:
            raise ValueError("Homogeneous graph record lacks executed replica evidence")
    if record.schema_version == MANAGER_CAMPAIGN_RECORD_SCHEMA:
        if (
            not record.planning_mode
            or not record.planning_reason
            or len(record.failure_reason_safe) > 1_024
            or len(record.employee_failure_codes) > 16
            or any(not code or len(code) > 128 for code in record.employee_failure_codes)
            or record.task_attempt_count < 0
            or record.successful_task_attempt_count < 0
            or record.successful_task_attempt_count > record.task_attempt_count
            or record.approvals_requested < 0
            or record.approvals_granted < 0
            or record.approvals_granted > record.approvals_requested
            or not math.isfinite(record.reported_cost_usd)
            or record.reported_cost_usd < 0
            or record.cost_accounting_mode not in {"REPORTED_USD", "MODEL_CALL_PROXY"}
            or record.validation_attempt_count < 0
            or record.validation_recovery_attempt_count < 0
            or record.validation_recovery_success_count < 0
            or record.validation_recovery_attempt_count
            > max(0, record.validation_attempt_count - 1)
            or record.validation_recovery_success_count
            > record.validation_recovery_attempt_count
            or (record.task_success and record.validation_attempt_count < 1)
            or record.runtime_user_intervention_count < 0
            or record.external_effect_error_count < 0
            or record.external_effect_unknown_count < 0
            or record.external_effect_unknown_count
            > record.external_effect_error_count
            or record.intervention_accounting_mode
            != "RUNTIME_OPERATOR_SIGNAL_LEDGER"
            or record.external_effect_accounting_mode
            != "DURABLE_TOOL_ACTION_STATUS"
        ):
            raise ValueError("Manager-value live record diagnostic summary is invalid")
    planning_facts = (
        record.manager_planning_owner_id,
        record.manager_planning_assignment_digest,
        record.manager_planning_brief_digest,
    )
    if arm == ManagerValueArm.MANAGER_LED_FIRM.value:
        if (
            record.manager_planning_owner_id != "manager-value-executive"
            or not record.compiler_planning_exercised
            or any(
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in planning_facts[1:]
            )
        ):
            raise ValueError("Manager-value record lacks exercised Manager planning provenance")
    elif any(planning_facts):
        raise ValueError("Non-Manager arm must not claim Manager planning provenance")


def seal_next_manager_value_slot(directory: str | Path, *, record_path: str | Path, confirm_live_quota: bool, confirm_evaluator_risk: bool) -> ManagerValueCampaignStatus:
    status = manager_value_campaign_status(directory)
    if status.state != CampaignState.READY or not status.next_fixture or not status.next_arm:
        raise ValueError("Manager-value campaign has no sealable slot")
    if not confirm_live_quota or not confirm_evaluator_risk:
        raise ValueError("Manager-value campaign requires quota and evaluator-risk confirmation for one slot")
    source = Path(record_path).expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise ValueError("Manager-value live record path is invalid")
    store, manifest = _artifacts(directory)
    try:
        _verify_runtime_inputs(store, manifest)
        record = _load_record(source)
        _validate_record(record, manifest, status.next_fixture, status.next_arm)
        store.append(CampaignEventKind.RUN_STARTED, fixture=status.next_fixture, strategy=status.next_arm, payload={"pid": os.getpid(), "quota_confirmed": True, "evaluator_risk_confirmed": True})
        relative = Path("records") / f"{status.completed_runs + 1:02d}-{status.next_fixture}-{status.next_arm}.json"
        target = _write_private(store.directory / relative, source.read_text(encoding="utf-8"))
        store.append(CampaignEventKind.RUN_RECORDED, fixture=status.next_fixture, strategy=status.next_arm, payload={"record_path": relative.as_posix(), "record_file_sha256": _sha256_file(target), "record_content_hash": record.content_hash, "task_success": record.task_success, "safety_passed": record.safety_passed, "external_model_calls": record.external_model_calls})
    finally:
        store.close()
    return manager_value_campaign_status(directory)


def _lower_decile(values: list[float]) -> float:
    if not values:
        raise ValueError("Manager-value report requires records")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.1) - 1)]


def create_manager_value_campaign_report(
    directory: str | Path,
    *,
    output_path: str | Path | None = None,
) -> ManagerValueCampaignReport:
    """Summarize a completed campaign without upgrading it into a learning patch."""

    status = manager_value_campaign_status(directory)
    if status.state != CampaignState.COMPLETE:
        raise ValueError(
            "Manager-value report requires all 16 sealed records; "
            f"state={status.state.value},completed={status.completed_runs}/{status.expected_runs}"
        )
    store, manifest = _artifacts(directory)
    try:
        events = {
            (event.fixture, event.strategy): event
            for event in store.events()
            if event.kind == CampaignEventKind.RUN_RECORDED
        }
        by_arm: dict[str, list[ManagerValueLiveRecord]] = {
            arm.value: [] for arm in ManagerValueArm
        }
        for fixture, arm in manifest.slots:
            event = events[(fixture, arm)]
            record = _load_record(store.directory / str(event.payload["record_path"]))
            _validate_record(record, manifest, fixture, arm)
            by_arm[arm].append(record)
        outcomes = tuple(
            ManagerValueArmOutcome(
                arm=arm.value,
                run_count=len(records),
                complete_failure_rate=sum(not item.task_success for item in records) / len(records),
                safety_failure_rate=sum(not item.safety_passed for item in records) / len(records),
                lower_decile_quality=_lower_decile([item.quality_score for item in records]),
                mean_quality=sum(item.quality_score for item in records) / len(records),
                mean_model_calls=sum(item.external_model_calls for item in records) / len(records),
                mean_elapsed_ms=sum(item.elapsed_ms for item in records) / len(records),
                mean_approvals_requested=(
                    sum(item.approvals_requested for item in records) / len(records)
                ),
                mean_approvals_granted=(
                    sum(item.approvals_granted for item in records) / len(records)
                ),
                mean_reported_cost_usd=(
                    sum(item.reported_cost_usd for item in records) / len(records)
                    if all(item.cost_accounting_mode == "REPORTED_USD" for item in records)
                    else None
                ),
                cost_accounting_mode=(
                    "REPORTED_USD"
                    if all(item.cost_accounting_mode == "REPORTED_USD" for item in records)
                    else "MODEL_CALL_PROXY"
                ),
                mean_validation_recovery_attempts=(
                    sum(item.validation_recovery_attempt_count for item in records)
                    / len(records)
                ),
                validation_recovery_success_rate=(
                    sum(item.validation_recovery_success_count for item in records)
                    / sum(
                        item.validation_recovery_attempt_count for item in records
                    )
                    if any(
                        item.validation_recovery_attempt_count for item in records
                    )
                    else None
                ),
                mean_runtime_user_interventions=(
                    sum(item.runtime_user_intervention_count for item in records)
                    / len(records)
                ),
                external_effect_error_rate=(
                    sum(item.external_effect_error_count > 0 for item in records)
                    / len(records)
                ),
                external_effect_unknown_rate=(
                    sum(item.external_effect_unknown_count > 0 for item in records)
                    / len(records)
                ),
            )
            for arm in ManagerValueArm
            for records in (by_arm[arm.value],)
        )
    finally:
        store.close()
    by_name = {item.arm: item for item in outcomes}
    manager = by_name[ManagerValueArm.MANAGER_LED_FIRM.value]
    heterogeneous = by_name[ManagerValueArm.HETEROGENEOUS_GRAPH.value]
    # Older records remain auditable, but do not establish the v6 failure and
    # approval-friction attribution contract needed for a current Manager
    # promotion claim.
    current_diagnostic_evidence = all(
        item.schema_version == MANAGER_CAMPAIGN_RECORD_SCHEMA
        for records in by_arm.values()
        for item in records
    )
    base = ManagerValueCampaignReport(
        schema_version="noruct.manager-value-campaign-report.v2",
        benchmark_id=manifest.benchmark_id,
        content_hash="pending",
        created_at=utc_now().isoformat(),
        qualified=current_diagnostic_evidence,
        outcomes=outcomes,
        manager_incremental_quality_vs_heterogeneous=(
            manager.mean_quality - heterogeneous.mean_quality
        ),
        manager_incremental_model_calls_vs_heterogeneous=(
            manager.mean_model_calls - heterogeneous.mean_model_calls
        ),
    )
    report = ManagerValueCampaignReport(
        schema_version=base.schema_version,
        benchmark_id=base.benchmark_id,
        content_hash=content_digest(base.content_payload()),
        created_at=base.created_at,
        qualified=base.qualified,
        outcomes=base.outcomes,
        manager_incremental_quality_vs_heterogeneous=(
            base.manager_incremental_quality_vs_heterogeneous
        ),
        manager_incremental_model_calls_vs_heterogeneous=(
            base.manager_incremental_model_calls_vs_heterogeneous
        ),
    )
    if output_path is not None:
        _write_private(
            Path(output_path).expanduser().resolve(),
            json.dumps(to_primitive(report), ensure_ascii=False, sort_keys=True, indent=2),
        )
    return report


async def run_next_manager_value_slot(
    directory: str | Path,
    *,
    confirm_live_quota: bool,
    confirm_evaluator_risk: bool,
    live_runner: Callable[..., Awaitable[ManagerValueLiveRecord]] | None = None,
    provider_factory=None,
    coding_worker_factory=None,
) -> ManagerValueCampaignStatus:
    """Reserve, execute and seal exactly one live four-way campaign slot.

    A slot is first appended as ``RUN_STARTED`` before any provider invocation.
    Therefore a process interruption can never cause silent slot reuse or an
    unaccounted provider call.  Failures are terminal campaign evidence too;
    a new campaign is required for a fresh comparable run.
    """

    status = manager_value_campaign_status(directory)
    if status.state != CampaignState.READY or not status.next_fixture or not status.next_arm:
        raise ValueError("Manager-value campaign has no runnable slot")
    if not confirm_live_quota or not confirm_evaluator_risk:
        raise ValueError("Manager-value campaign requires quota and evaluator-risk confirmation for one slot")
    store, manifest = _artifacts(directory)
    try:
        _verify_runtime_inputs(store, manifest)
        start = store.append(
            CampaignEventKind.RUN_STARTED,
            fixture=status.next_fixture,
            strategy=status.next_arm,
            payload={
                "pid": os.getpid(),
                "quota_confirmed": True,
                "evaluator_risk_confirmed": True,
                "max_model_calls": manifest.max_total_model_calls,
                "max_wall_time_ms": manifest.max_wall_time_ms,
                "executor": "in-process-firm-kernel",
            },
        )
        metadata = store.metadata()
    finally:
        store.close()

    from .manager_value_live import ManagerValueLiveConfig, run_live_manager_value_evaluation

    config = ManagerValueLiveConfig(
        command=str(metadata["codex_command"]),
        model=manifest.model_id,
        source_revision=manifest.source_revision,
        distribution_sha256=manifest.distribution_sha256,
        timeout_seconds=float(metadata["request_timeout_seconds"]),
        max_total_model_calls=manifest.max_total_model_calls,
        max_wall_time_ms=manifest.max_wall_time_ms,
        quota_confirmed=True,
        evaluator_risk_confirmed=True,
        company_revision=manifest.company_revision,
        roster_revision=manifest.roster_revision,
        playbook_revision=manifest.playbook_revision,
    )
    runner = live_runner or run_live_manager_value_evaluation
    failure_stage = "LIVE_RUNNER"
    try:
        record = await runner(
            config,
            status.next_fixture,
            status.next_arm,
            provider_factory=provider_factory,
            coding_worker_factory=coding_worker_factory,
        )
        failure_stage = "RECORD_VALIDATION"
        _validate_record(record, manifest, status.next_fixture, status.next_arm)
        failure_stage = "RECORD_SEAL"
        relative = Path("records") / (
            f"{start.sequence:02d}-{status.next_fixture}-{status.next_arm}.json"
        )
        target = _write_private(
            Path(directory).expanduser().resolve() / relative,
            json.dumps(to_primitive(record), ensure_ascii=False, sort_keys=True, indent=2),
        )
        with ManagerValueCampaignStore(directory) as completed:
            completed.append(
                CampaignEventKind.RUN_RECORDED,
                fixture=status.next_fixture,
                strategy=status.next_arm,
                payload={
                    "record_path": relative.as_posix(),
                    "record_file_sha256": _sha256_file(target),
                    "record_content_hash": record.content_hash,
                    "task_success": record.task_success,
                    "safety_passed": record.safety_passed,
                    "external_model_calls": record.external_model_calls,
                    "executor": "in-process-firm-kernel",
                },
            )
    except BaseException as exc:
        interrupted = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
        kind = CampaignEventKind.RUN_INTERRUPTED if interrupted else CampaignEventKind.RUN_FAILED
        relative = Path("failures") / (
            f"{start.sequence:02d}-{status.next_fixture}-{status.next_arm}.json"
        )
        failure_payload = {
            "schema_version": "noruct.manager-value-failure.v1",
            "benchmark_id": status.benchmark_id,
            "fixture": status.next_fixture,
            "arm": status.next_arm,
            "recorded_at": utc_now().isoformat(),
            "failure_code": type(exc).__name__,
            "failure_stage": failure_stage,
            # Contract errors are fixed implementation messages.  Retain a
            # bounded diagnostic so a consumed live slot is explainable,
            # without recording model output, prompts, or workspace content.
            "failure_detail": str(exc)[:240],
            "interrupted": interrupted,
            "quota_confirmed": True,
            "evaluator_risk_confirmed": True,
            "external_model_calls_accounting": "UNKNOWN_FORFEITED",
            "reserved_external_model_calls": manifest.max_total_model_calls,
        }
        failure_path = _write_private(
            Path(directory).expanduser().resolve() / relative,
            json.dumps(failure_payload, ensure_ascii=False, sort_keys=True, indent=2),
        )
        with ManagerValueCampaignStore(directory) as failed:
            failed.append(
                kind,
                fixture=status.next_fixture,
                strategy=status.next_arm,
                payload={
                    "failure_path": relative.as_posix(),
                    "failure_file_sha256": _sha256_file(failure_path),
                    "failure_code": type(exc).__name__,
                    "failure_stage": failure_stage,
                    "external_model_calls_accounting": "UNKNOWN_FORFEITED",
                    "reserved_external_model_calls": manifest.max_total_model_calls,
                },
            )
        if interrupted:
            raise
    return manager_value_campaign_status(directory)
