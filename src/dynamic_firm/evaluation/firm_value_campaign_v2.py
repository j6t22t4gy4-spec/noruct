from __future__ import annotations

import asyncio
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

from .closed_loop import CodingStrategyKind
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
from .firm_value_v2 import (
    FIRM_VALUE_V2_EVALUATOR_PROFILE,
    FIRM_VALUE_V2_LIVE_EVIDENCE_CLASS,
    QUALITY_GAIN_THRESHOLD,
    FirmValueV2FixtureKind,
    FirmValueV2Report,
    FixturePurpose,
    LiveFirmValueV2Config,
    LiveFirmValueV2Record,
    compare_firm_value_v2_records,
    firm_value_v2_fixture_contract,
    firm_value_v2_to_json,
    fixture_purpose,
    load_live_firm_value_v2_record,
    run_firm_value_v2_self_test,
    run_live_firm_value_v2_evaluation,
)


CAMPAIGN_V2_MANIFEST_SCHEMA = "noruct.firm-value-campaign-manifest.v2"
CAMPAIGN_V2_PREFLIGHT_SCHEMA = "noruct.firm-value-campaign-preflight.v2"
CAMPAIGN_V2_STATUS_SCHEMA = "noruct.firm-value-campaign-status.v2"
CAMPAIGN_V2_LEDGER_SCHEMA = "noruct.firm-value-campaign-ledger.v2"
CAMPAIGN_V2_FAILURE_SCHEMA = "noruct.firm-value-campaign-failure.v2"
CAMPAIGN_V2_COMPARISON_SCHEMA = "noruct.firm-value-campaign-comparison.v2"
_CAMPAIGN_V2_DB = "campaign-v2.db"


@dataclass(frozen=True, slots=True)
class CampaignV2FixtureSpec:
    fixture: str
    purpose: str
    fixture_revision: str
    validation_command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CampaignV2ExpectedRun:
    fixture: str
    strategy: str


@dataclass(frozen=True, slots=True)
class FirmValueCampaignV2Manifest:
    schema_version: str
    benchmark_id: str
    content_hash: str
    created_at: str
    expires_at: str
    noruct_version: str
    distribution_sha256: str
    source_revision: str
    provider_kind: str
    model_id: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    permission_mode: str
    approval_mode: str
    max_total_model_calls: int
    max_wall_time_ms: int
    quality_gain_threshold: float
    evaluator_profile: str
    evaluator_network_isolated: bool
    evaluator_credential_inheritance: bool
    evaluator_risk_confirmation_required: bool
    fixtures: tuple[CampaignV2FixtureSpec, ...]
    expected_runs: tuple[CampaignV2ExpectedRun, ...]

    def content_payload(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "noruct_version": self.noruct_version,
            "distribution_sha256": self.distribution_sha256,
            "source_revision": self.source_revision,
            "provider_kind": self.provider_kind,
            "model_id": self.model_id,
            "company_revision": self.company_revision,
            "roster_revision": self.roster_revision,
            "playbook_revision": self.playbook_revision,
            "permission_mode": self.permission_mode,
            "approval_mode": self.approval_mode,
            "max_total_model_calls": self.max_total_model_calls,
            "max_wall_time_ms": self.max_wall_time_ms,
            "quality_gain_threshold": self.quality_gain_threshold,
            "evaluator_profile": self.evaluator_profile,
            "evaluator_network_isolated": self.evaluator_network_isolated,
            "evaluator_credential_inheritance": self.evaluator_credential_inheritance,
            "evaluator_risk_confirmation_required": self.evaluator_risk_confirmation_required,
            "fixtures": self.fixtures,
            "expected_runs": self.expected_runs,
        }


@dataclass(frozen=True, slots=True)
class FirmValueCampaignV2Check:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class FirmValueCampaignV2Preflight:
    schema_version: str
    benchmark_id: str
    recorded_at: str
    source_revision: str
    distribution_sha256: str
    provider_kind: str
    model_id: str
    offline_runs_checked: int
    external_model_calls: int
    quota_consumed: bool
    evaluator_profile: str
    evaluator_network_isolated: bool
    evaluator_credential_inheritance: bool
    evaluator_risk_confirmation_required: bool
    ready: bool
    checks: tuple[FirmValueCampaignV2Check, ...]


@dataclass(frozen=True, slots=True)
class FirmValueCampaignV2Status:
    schema_version: str
    benchmark_id: str
    state: CampaignState
    manifest_content_hash: str
    manifest_fresh: bool
    viable: bool
    stop_reason: str | None
    completed_runs: int
    expected_runs: int
    failed_runs: int
    interrupted_runs: int
    next_fixture: str | None
    next_strategy: str | None
    max_model_calls_for_next_run: int
    max_wall_time_ms_for_next_run: int
    explicit_quota_confirmation_required: bool
    explicit_evaluator_risk_confirmation_required: bool
    evaluator_profile: str
    external_model_calls_recorded: int
    event_count: int
    ledger_verified: bool
    record_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FirmValueCampaignV2Preparation:
    preflight: FirmValueCampaignV2Preflight
    status: FirmValueCampaignV2Status


@dataclass(frozen=True, slots=True)
class FirmValueCampaignV2RunResult:
    event: FirmValueCampaignEvent
    status: FirmValueCampaignV2Status
    record_path: str | None
    task_success: bool


@dataclass(frozen=True, slots=True)
class FirmValueCampaignV2Comparison:
    schema_version: str
    benchmark_id: str
    manifest_content_hash: str
    completed_runs: int
    expected_runs: int
    safety_gate_passed: bool
    control_gate_passed: bool
    organization_gate_passed: bool
    value_fixture_count: int
    value_gain_count: int
    manifest_budget_gate_passed: bool
    campaign_gate_passed: bool
    outcome: str
    recommended_direction: str
    aggregator_provider_calls: int
    aggregator_quota_consumed: bool
    aggregate_report: FirmValueV2Report


class FirmValueCampaignV2Store(FirmValueCampaignStore):
    def __init__(self, directory: str | Path, *, create: bool = False) -> None:
        root = Path(directory).expanduser().resolve()
        if not create and (root / "campaign.db").exists() and not (
            root / _CAMPAIGN_V2_DB
        ).exists():
            raise ValueError("Firm-value campaign v2 refuses a v1 campaign directory")
        super().__init__(
            root,
            create=create,
            db_name=_CAMPAIGN_V2_DB,
            ledger_schema=CAMPAIGN_V2_LEDGER_SCHEMA,
            event_id_prefix="campaign-v2-event",
        )


def firm_value_campaign_v2_expected_runs() -> tuple[tuple[str, str], ...]:
    return tuple(
        (fixture.value, strategy.value)
        for fixture in FirmValueV2FixtureKind
        for strategy in (CodingStrategyKind.SOLO, CodingStrategyKind.DYNAMIC)
    )


def _create_manifest(
    *,
    distribution_sha256: str,
    source_revision: str,
    model_id: str,
    company_revision: int,
    roster_revision: int,
    playbook_revision: int,
    max_total_model_calls: int,
    max_wall_time_ms: int,
    lifetime_hours: int,
) -> FirmValueCampaignV2Manifest:
    if len(distribution_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in distribution_sha256
    ):
        raise ValueError("Firm-value campaign v2 wheel SHA-256 is invalid")
    if not source_revision.startswith("snapshot-sha256:") or not model_id.strip():
        raise ValueError("Firm-value campaign v2 requires frozen source and explicit model")
    if not 4 <= max_total_model_calls <= 8:
        raise ValueError("Firm-value campaign v2 model-call limit must be between 4 and 8")
    if not 1_000 <= max_wall_time_ms <= 600_000 or not 1 <= lifetime_hours <= 336:
        raise ValueError("Firm-value campaign v2 time bounds are invalid")
    revisions = (company_revision, roster_revision, playbook_revision)
    if any(type(value) is not int or value < 0 for value in revisions):
        raise ValueError("Firm-value campaign v2 revisions must be non-negative integers")
    created = utc_now().astimezone(timezone.utc)
    expires = created + timedelta(hours=lifetime_hours)
    fixtures = tuple(
        CampaignV2FixtureSpec(
            fixture=fixture.value,
            purpose=fixture_purpose(fixture).value,
            fixture_revision=firm_value_v2_fixture_contract(fixture).fixture_revision,
            validation_command=firm_value_v2_fixture_contract(fixture).validation_command,
        )
        for fixture in FirmValueV2FixtureKind
    )
    expected = tuple(
        CampaignV2ExpectedRun(fixture=fixture, strategy=strategy)
        for fixture, strategy in firm_value_campaign_v2_expected_runs()
    )
    base = FirmValueCampaignV2Manifest(
        schema_version=CAMPAIGN_V2_MANIFEST_SCHEMA,
        benchmark_id="pending",
        content_hash="pending",
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        noruct_version=__version__,
        distribution_sha256=distribution_sha256,
        source_revision=source_revision,
        provider_kind="openai-codex-user-managed",
        model_id=model_id.strip(),
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
        permission_mode="shadow-workspace-approved",
        approval_mode="allow-once",
        max_total_model_calls=max_total_model_calls,
        max_wall_time_ms=max_wall_time_ms,
        quality_gain_threshold=QUALITY_GAIN_THRESHOLD,
        evaluator_profile=FIRM_VALUE_V2_EVALUATOR_PROFILE,
        evaluator_network_isolated=False,
        evaluator_credential_inheritance=False,
        evaluator_risk_confirmation_required=True,
        fixtures=fixtures,
        expected_runs=expected,
    )
    digest = content_digest(base.content_payload())
    return FirmValueCampaignV2Manifest(
        **{
            **to_primitive(base),
            "benchmark_id": f"firm-value-v2-{digest[:24]}",
            "content_hash": digest,
            "fixtures": fixtures,
            "expected_runs": expected,
        }
    )


def _manifest_to_json(manifest: FirmValueCampaignV2Manifest) -> str:
    return json.dumps(to_primitive(manifest), ensure_ascii=False, sort_keys=True, indent=2)


def _load_manifest(path: Path) -> FirmValueCampaignV2Manifest:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Firm-value campaign v2 manifest is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != CAMPAIGN_V2_MANIFEST_SCHEMA:
        raise ValueError("Firm-value campaign v2 refuses non-v2 manifests")
    fixtures = tuple(
        CampaignV2FixtureSpec(
            fixture=str(item["fixture"]),
            purpose=str(item["purpose"]),
            fixture_revision=str(item["fixture_revision"]),
            validation_command=tuple(str(part) for part in item["validation_command"]),
        )
        for item in value["fixtures"]
    )
    expected = tuple(CampaignV2ExpectedRun(**item) for item in value["expected_runs"])
    manifest = FirmValueCampaignV2Manifest(
        **{
            **{key: item for key, item in value.items() if key not in {"fixtures", "expected_runs"}},
            "fixtures": fixtures,
            "expected_runs": expected,
        }
    )
    if (
        manifest.noruct_version != __version__
        or manifest.content_hash != content_digest(manifest.content_payload())
        or manifest.benchmark_id != f"firm-value-v2-{manifest.content_hash[:24]}"
        or tuple((item.fixture, item.strategy) for item in expected)
        != firm_value_campaign_v2_expected_runs()
        or len(fixtures) != 4
        or manifest.quality_gain_threshold != QUALITY_GAIN_THRESHOLD
        or manifest.evaluator_profile != FIRM_VALUE_V2_EVALUATOR_PROFILE
        or manifest.evaluator_network_isolated
        or manifest.evaluator_credential_inheritance
        or not manifest.evaluator_risk_confirmation_required
    ):
        raise ValueError("Firm-value campaign v2 manifest contract is invalid")
    for spec, fixture in zip(fixtures, FirmValueV2FixtureKind, strict=True):
        contract = firm_value_v2_fixture_contract(fixture)
        if (
            spec.fixture != fixture.value
            or spec.purpose != contract.purpose.value
            or spec.fixture_revision != contract.fixture_revision
            or spec.validation_command != contract.validation_command
        ):
            raise ValueError("Firm-value campaign v2 fixture contract changed")
    return manifest


def _load_preflight(path: Path) -> FirmValueCampaignV2Preflight:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Firm-value campaign v2 preflight is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != CAMPAIGN_V2_PREFLIGHT_SCHEMA:
        raise ValueError("Firm-value campaign v2 refuses non-v2 preflight records")
    checks = tuple(FirmValueCampaignV2Check(**item) for item in value["checks"])
    return FirmValueCampaignV2Preflight(
        **{key: item for key, item in value.items() if key != "checks"},
        checks=checks,
    )


def _campaign_artifacts(
    store: FirmValueCampaignV2Store,
) -> tuple[
    dict[str, object],
    Path,
    FirmValueCampaignV2Manifest,
    FirmValueCampaignV2Preflight,
]:
    metadata = store.metadata()
    if metadata.get("schema_version") != CAMPAIGN_V2_LEDGER_SCHEMA:
        raise ValueError("Firm-value campaign v2 ledger schema is invalid")
    manifest_path = store.directory / "manifest-v2.json"
    preflight_path = store.directory / "preflight-v2.json"
    if _sha256_file(manifest_path) != metadata.get("manifest_file_sha256"):
        raise ValueError("Firm-value campaign v2 manifest hash changed")
    if _sha256_file(preflight_path) != metadata.get("preflight_file_sha256"):
        raise ValueError("Firm-value campaign v2 preflight hash changed")
    manifest = _load_manifest(manifest_path)
    preflight = _load_preflight(preflight_path)
    if (
        manifest.benchmark_id != metadata.get("benchmark_id")
        or preflight.benchmark_id != manifest.benchmark_id
    ):
        raise ValueError("Firm-value campaign v2 identity does not match its ledger")
    return metadata, manifest_path, manifest, preflight


def _manifest_fresh(manifest: FirmValueCampaignV2Manifest) -> bool:
    try:
        expires = datetime.fromisoformat(manifest.expires_at).astimezone(timezone.utc)
    except ValueError:
        return False
    return utc_now().astimezone(timezone.utc) <= expires


def _sealed_artifact_path(root: Path, relative: object, folder: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("Firm-value campaign v2 sealed artifact path is invalid")
    unresolved = root / relative
    candidate = unresolved.resolve()
    boundary = (root / folder).resolve()
    if (
        Path(relative).is_absolute()
        or candidate.parent != boundary
        or not candidate.is_file()
        or unresolved.is_symlink()
    ):
        raise ValueError("Firm-value campaign v2 sealed artifact escaped its directory")
    return candidate


def campaign_v2_status(directory: str | Path) -> FirmValueCampaignV2Status:
    with FirmValueCampaignV2Store(directory) as store:
        _, _, manifest, preflight = _campaign_artifacts(store)
        events = store.events()
        root = store.directory
    slots = firm_value_campaign_v2_expected_runs()
    started: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    recorded: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    failed: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    interrupted_terminal: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    qualified_records: dict[tuple[str, str], LiveFirmValueV2Record] = {}
    for event in events:
        if event.fixture is None or event.strategy is None:
            continue
        key = (event.fixture, event.strategy)
        if key not in slots:
            raise ValueError("Firm-value campaign v2 ledger contains an unknown slot")
        if event.kind == CampaignEventKind.RUN_STARTED:
            if key in started:
                raise ValueError("Firm-value campaign v2 reuses a run slot")
            started[key] = event
        elif event.kind == CampaignEventKind.RUN_RECORDED:
            if key not in started or key in recorded or key in failed:
                raise ValueError("Firm-value campaign v2 record has no unique start")
            recorded[key] = event
        elif event.kind in {CampaignEventKind.RUN_FAILED, CampaignEventKind.RUN_INTERRUPTED}:
            if key not in started or key in recorded or key in failed or key in interrupted_terminal:
                raise ValueError("Firm-value campaign v2 failure has no unique start")
            if event.kind == CampaignEventKind.RUN_INTERRUPTED:
                interrupted_terminal[key] = event
            else:
                failed[key] = event
    for key, event in recorded.items():
        path = _sealed_artifact_path(root, event.payload.get("record_path"), "records-v2")
        if _sha256_file(path) != event.payload.get("record_file_sha256"):
            raise ValueError("Firm-value campaign v2 sealed record changed")
        record = _validate_live_record(
            path,
            manifest,
            expected_fixture=key[0],
            expected_strategy=key[1],
        )
        qualified_records[key] = record
        if (
            record.content_hash != event.payload.get("record_content_hash")
            or record.evaluation_run_id != event.payload.get("evaluation_run_id")
            or record.result.status != event.payload.get("status")
            or record.result.task_success != event.payload.get("task_success")
            or record.external_model_calls != event.payload.get("external_model_calls")
        ):
            raise ValueError("Firm-value campaign v2 sealed record ledger projection changed")
    for event in (*failed.values(), *interrupted_terminal.values()):
        path = _sealed_artifact_path(
            root, event.payload.get("failure_path"), "failures-v2"
        )
        if _sha256_file(path) != event.payload.get("failure_file_sha256"):
            raise ValueError("Firm-value campaign v2 sealed failure changed")
    open_slots = {
        key: event
        for key, event in started.items()
        if key not in recorded and key not in failed and key not in interrupted_terminal
    }
    abandoned = sum(
        not _process_is_alive(event.payload.get("pid")) for event in open_slots.values()
    )
    interrupted = len(interrupted_terminal) + abandoned
    running = len(open_slots) - abandoned
    fresh = _manifest_fresh(manifest)
    stop_reason = next(
        (
            (
                f"SAFETY_GATE_IMPOSSIBLE:{fixture}/{strategy}"
                if not record.result.safety.passed
                else f"CONTROL_GATE_IMPOSSIBLE:{fixture}/{strategy}"
            )
            for (fixture, strategy), record in qualified_records.items()
            if not record.result.safety.passed
            or (
                record.result.purpose == FixturePurpose.CONTROL
                and not record.result.task_success
            )
        ),
        None,
    )
    if stop_reason is None and failed:
        failed_fixture, failed_strategy = next(iter(failed))
        stop_reason = f"RUN_FAILURE:{failed_fixture}/{failed_strategy}"
    if failed or stop_reason is not None:
        state = CampaignState.PARTIAL_FAILED
    elif interrupted:
        state = CampaignState.INTERRUPTED
    elif running:
        state = CampaignState.RUNNING
    elif len(recorded) == len(slots):
        state = CampaignState.COMPLETE
    elif not preflight.ready or not fresh:
        state = CampaignState.BLOCKED
    else:
        state = CampaignState.READY
    next_slot = (
        next((slot for slot in slots if slot not in recorded), None)
        if state == CampaignState.READY
        else None
    )
    return FirmValueCampaignV2Status(
        schema_version=CAMPAIGN_V2_STATUS_SCHEMA,
        benchmark_id=manifest.benchmark_id,
        state=state,
        manifest_content_hash=manifest.content_hash,
        manifest_fresh=fresh,
        viable=stop_reason is None,
        stop_reason=stop_reason,
        completed_runs=len(recorded),
        expected_runs=len(slots),
        failed_runs=len(failed),
        interrupted_runs=interrupted,
        next_fixture=next_slot[0] if next_slot else None,
        next_strategy=next_slot[1] if next_slot else None,
        max_model_calls_for_next_run=manifest.max_total_model_calls if next_slot else 0,
        max_wall_time_ms_for_next_run=manifest.max_wall_time_ms if next_slot else 0,
        explicit_quota_confirmation_required=next_slot is not None,
        explicit_evaluator_risk_confirmation_required=next_slot is not None,
        evaluator_profile=manifest.evaluator_profile,
        external_model_calls_recorded=sum(
            int(event.payload.get("external_model_calls", 0))
            for event in recorded.values()
        ),
        event_count=len(events),
        ledger_verified=True,
        record_paths=tuple(
            str(root / str(recorded[slot].payload["record_path"]))
            for slot in slots
            if slot in recorded
        ),
    )


async def prepare_firm_value_campaign_v2(
    directory: str | Path,
    *,
    wheel: str | Path,
    source_root: str | Path,
    command: str,
    model_id: str,
    company_revision: int = 0,
    roster_revision: int = 0,
    playbook_revision: int = 0,
    max_total_model_calls: int = 4,
    max_wall_time_ms: int = 180_000,
    lifetime_hours: int = 168,
    request_timeout_seconds: float = 120.0,
    login_status_factory: Callable[[str], CodexLoginStatus] | None = None,
    capability_probe: Callable[[str], tuple[str | None, bool, str]] | None = None,
) -> FirmValueCampaignV2Preparation:
    target = Path(directory).expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f"Firm-value campaign v2 directory must be empty: {target}")
    source = Path(source_root).expanduser().resolve()
    wheel_path = Path(wheel).expanduser().resolve()
    source_revision = source_snapshot_revision(source)
    distribution_sha256 = wheel_distribution_sha256(wheel_path)
    manifest = _create_manifest(
        distribution_sha256=distribution_sha256,
        source_revision=source_revision,
        model_id=model_id,
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
        max_total_model_calls=max_total_model_calls,
        max_wall_time_ms=max_wall_time_ms,
        lifetime_hours=lifetime_hours,
    )
    offline = await run_firm_value_v2_self_test()
    login = (login_status_factory or CodexExecProvider.login_status)(command)
    executable, structured_supported, capability_evidence = (
        capability_probe or probe_codex_structured_output
    )(command)
    portable_commands = all(
        spec.validation_command[0] == "<python>"
        and spec.validation_command[-1] == "<workspace>"
        and not any(Path(part).is_absolute() for part in spec.validation_command)
        for spec in manifest.fixtures
    )
    checks = (
        FirmValueCampaignV2Check("source-snapshot-frozen", True, source_revision),
        FirmValueCampaignV2Check("wheel-hash-frozen", True, distribution_sha256),
        FirmValueCampaignV2Check(
            "codex-executable-installed",
            bool(login.installed and login.executable and executable),
            executable or login.executable or command,
        ),
        FirmValueCampaignV2Check(
            "codex-authenticated",
            bool(login.authenticated),
            "official login status passed" if login.authenticated else "authentication not confirmed",
        ),
        FirmValueCampaignV2Check(
            "structured-output-cli-contract",
            structured_supported,
            capability_evidence,
        ),
        FirmValueCampaignV2Check(
            "portable-first-party-validation",
            portable_commands,
            f"fixtures={len(manifest.fixtures)}",
        ),
        FirmValueCampaignV2Check(
            "offline-v2-contract",
            offline.passed and offline.report.ready_for_live_preflight,
            f"runs=8,value={offline.report.value_gain_count}/2",
        ),
        FirmValueCampaignV2Check(
            "fixture-purpose-boundary",
            sum(spec.purpose == FixturePurpose.CONTROL.value for spec in manifest.fixtures) == 2
            and sum(
                spec.purpose == FixturePurpose.VALUE_IDENTIFIABLE.value
                for spec in manifest.fixtures
            )
            == 2,
            "control=2,value-identifiable=2",
        ),
        FirmValueCampaignV2Check(
            "evaluator-risk-disclosed",
            manifest.evaluator_profile == FIRM_VALUE_V2_EVALUATOR_PROFILE
            and not manifest.evaluator_network_isolated
            and not manifest.evaluator_credential_inheritance
            and manifest.evaluator_risk_confirmation_required,
            "no-os-sandbox,network-not-isolated,clean-env,per-run-confirmation-required",
        ),
        FirmValueCampaignV2Check(
            "one-run-quota-bound",
            4 <= max_total_model_calls <= 8
            and 1_000 <= max_wall_time_ms <= 600_000
            and request_timeout_seconds > 0,
            f"calls<={max_total_model_calls},wall_ms<={max_wall_time_ms}",
        ),
    )
    preflight = FirmValueCampaignV2Preflight(
        schema_version=CAMPAIGN_V2_PREFLIGHT_SCHEMA,
        benchmark_id=manifest.benchmark_id,
        recorded_at=utc_now().isoformat(),
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        provider_kind=manifest.provider_kind,
        model_id=manifest.model_id,
        offline_runs_checked=8,
        external_model_calls=0,
        quota_consumed=False,
        evaluator_profile=manifest.evaluator_profile,
        evaluator_network_isolated=manifest.evaluator_network_isolated,
        evaluator_credential_inheritance=manifest.evaluator_credential_inheritance,
        evaluator_risk_confirmation_required=True,
        ready=all(check.passed for check in checks),
        checks=checks,
    )
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    manifest_path = _write_private(target / "manifest-v2.json", _manifest_to_json(manifest))
    preflight_path = _write_private(
        target / "preflight-v2.json",
        json.dumps(to_primitive(preflight), ensure_ascii=False, sort_keys=True, indent=2),
    )
    with FirmValueCampaignV2Store(target, create=True) as store:
        store.initialize(
            {
                "schema_version": CAMPAIGN_V2_LEDGER_SCHEMA,
                "benchmark_id": manifest.benchmark_id,
                "manifest_content_hash": manifest.content_hash,
                "manifest_file_sha256": _sha256_file(manifest_path),
                "preflight_file_sha256": _sha256_file(preflight_path),
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
                "offline_runs_checked": 8,
                "external_model_calls": 0,
                "quota_consumed": False,
                "evaluator_risk_confirmation_required": True,
            },
        )
    return FirmValueCampaignV2Preparation(preflight, campaign_v2_status(target))


def _verify_runtime_inputs(
    metadata: Mapping[str, object],
    manifest: FirmValueCampaignV2Manifest,
) -> None:
    if source_snapshot_revision(Path(str(metadata["source_root"]))) != manifest.source_revision:
        raise ValueError("Firm-value campaign v2 source snapshot changed after preparation")
    if wheel_distribution_sha256(Path(str(metadata["wheel_path"]))) != manifest.distribution_sha256:
        raise ValueError("Firm-value campaign v2 wheel changed after preparation")


def _validate_live_record(
    path: Path,
    manifest: FirmValueCampaignV2Manifest,
    *,
    expected_fixture: str,
    expected_strategy: str,
) -> LiveFirmValueV2Record:
    record = load_live_firm_value_v2_record(path)
    result = record.result
    fixture_specs = {spec.fixture: spec for spec in manifest.fixtures}
    spec = fixture_specs.get(expected_fixture)
    if spec is None:
        raise ValueError("Firm-value campaign v2 live record fixture is unknown")
    if (
        result.fixture.value != expected_fixture
        or result.strategy.value != expected_strategy
        or result.purpose.value != spec.purpose
        or result.fixture_revision != spec.fixture_revision
        or result.evidence_class != FIRM_VALUE_V2_LIVE_EVIDENCE_CLASS
        or record.source_revision != manifest.source_revision
        or record.distribution_sha256 != manifest.distribution_sha256
        or record.model_id != manifest.model_id
        or record.company_revision != manifest.company_revision
        or record.roster_revision != manifest.roster_revision
        or record.playbook_revision != manifest.playbook_revision
        or record.permission_mode != manifest.permission_mode
        or record.approval_mode != manifest.approval_mode
        or record.configured_model_call_limit != manifest.max_total_model_calls
        or record.configured_wall_time_ms != manifest.max_wall_time_ms
        or record.external_model_calls > manifest.max_total_model_calls
        or result.cost.measured_elapsed_ms != record.elapsed_ms
    ):
        raise ValueError("Firm-value campaign v2 live record violates the manifest")
    return record


from .firm_value_campaign_v2_execution import run_next_campaign_v2_slot  # noqa: E402


def compare_campaign_v2(
    directory: str | Path,
    *,
    output_path: str | Path | None = None,
) -> FirmValueCampaignV2Comparison:
    status = campaign_v2_status(directory)
    if status.state != CampaignState.COMPLETE:
        raise ValueError(
            "Firm-value campaign v2 comparison requires eight sealed records; "
            f"state={status.state.value},completed={status.completed_runs}/{status.expected_runs}"
        )
    root = Path(directory).expanduser().resolve()
    with FirmValueCampaignV2Store(root) as store:
        _, _, manifest, _ = _campaign_artifacts(store)
        events = store.events()
        recorded = {
            (event.fixture, event.strategy): event
            for event in events
            if event.kind == CampaignEventKind.RUN_RECORDED
        }
        live_records: list[LiveFirmValueV2Record] = []
        for fixture, strategy in firm_value_campaign_v2_expected_runs():
            event = recorded[(fixture, strategy)]
            path = root / str(event.payload["record_path"])
            if _sha256_file(path) != event.payload.get("record_file_sha256"):
                raise ValueError("Firm-value campaign v2 sealed record changed")
            live_records.append(
                _validate_live_record(
                    path,
                    manifest,
                    expected_fixture=fixture,
                    expected_strategy=strategy,
                )
            )
        if len({record.evaluation_run_id for record in live_records}) != 8 or len(
            {record.content_hash for record in live_records}
        ) != 8:
            raise ValueError("Firm-value campaign v2 live identities are not unique")
        aggregate = compare_firm_value_v2_records(
            tuple(record.result for record in live_records)
        )
        campaign_gate = aggregate.ready_for_live_preflight
        if not aggregate.safety_gate_passed:
            outcome = "SAFETY_GATE_FAILED"
            direction = "FREEZE_AND_FIX_SAFETY"
        elif not aggregate.control_gate_passed:
            outcome = "CONTROL_REGRESSION"
            direction = "FREEZE_AND_FIX_LIVE_CONTRACT"
        elif not aggregate.organization_gate_passed:
            outcome = "ORGANIZATION_ATTRIBUTION_FAILED"
            direction = "NARROW_TO_SOLO_FIRST"
        elif campaign_gate:
            outcome = "DYNAMIC_VALUE_GATE_PASSED"
            direction = "REPEAT_BEFORE_FEATURE_EXPANSION"
        else:
            outcome = "DYNAMIC_VALUE_GATE_NOT_MET"
            direction = "FREEZE_ORGANIZATION_FEATURES"
        comparison = FirmValueCampaignV2Comparison(
            schema_version=CAMPAIGN_V2_COMPARISON_SCHEMA,
            benchmark_id=manifest.benchmark_id,
            manifest_content_hash=manifest.content_hash,
            completed_runs=8,
            expected_runs=8,
            safety_gate_passed=aggregate.safety_gate_passed,
            control_gate_passed=aggregate.control_gate_passed,
            organization_gate_passed=aggregate.organization_gate_passed,
            value_fixture_count=aggregate.value_fixture_count,
            value_gain_count=aggregate.value_gain_count,
            manifest_budget_gate_passed=True,
            campaign_gate_passed=campaign_gate,
            outcome=outcome,
            recommended_direction=direction,
            aggregator_provider_calls=0,
            aggregator_quota_consumed=False,
            aggregate_report=aggregate,
        )
        target = (
            Path(output_path).expanduser().resolve()
            if output_path
            else root / "report-v2.json"
        )
        _write_private(
            target,
            json.dumps(to_primitive(comparison), ensure_ascii=False, sort_keys=True, indent=2),
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
