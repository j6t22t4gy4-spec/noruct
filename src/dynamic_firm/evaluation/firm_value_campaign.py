from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from dynamic_firm.company.models import content_digest
from dynamic_firm.providers.codex_exec import CodexExecProvider, CodexLoginStatus
from dynamic_firm.runtime.models import to_primitive, utc_now
from dynamic_firm.runtime.ports import ModelProviderError, OperationCancelled

from .closed_loop import (
    LiveCodingEvaluationConfig,
    LiveCodingEvaluationRecord,
    live_coding_record_to_json,
    run_closed_loop_evaluation,
    run_live_coding_evaluation,
)
from .firm_value import (
    FirmValueManifest,
    FirmValueReport,
    QUALITY_GAIN_THRESHOLD,
    aggregate_firm_value_records,
    create_firm_value_manifest,
    firm_value_expected_runs,
    firm_value_manifest_to_json,
    load_firm_value_manifest,
    validate_firm_value_record,
    wheel_distribution_sha256,
)
from .firm_value_campaign_source import (
    _canonical,
    _sha256_file,
    _write_private,
    probe_codex_structured_output,
    source_snapshot_revision,
)


CAMPAIGN_PREFLIGHT_SCHEMA = "noruct.firm-value-campaign-preflight.v1"
CAMPAIGN_STATUS_SCHEMA = "noruct.firm-value-campaign-status.v1"
CAMPAIGN_LEDGER_SCHEMA = "noruct.firm-value-campaign-ledger.v1"
CAMPAIGN_COMPARISON_SCHEMA = "noruct.firm-value-campaign-comparison.v1"
SOURCE_SNAPSHOT_PREFIX = "snapshot-sha256:"
_MAX_SOURCE_FILES = 8_000
_MAX_SOURCE_BYTES = 100_000_000
_SNAPSHOT_TOP_LEVEL = (
    "LICENSE",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
)


class CampaignEventKind(StrEnum):
    PREPARED = "PREPARED"
    RUN_STARTED = "RUN_STARTED"
    RUN_RECORDED = "RUN_RECORDED"
    RUN_FAILED = "RUN_FAILED"
    RUN_INTERRUPTED = "RUN_INTERRUPTED"
    ASSESSMENT_RECORDED = "ASSESSMENT_RECORDED"
    ROLLBACK_RECORDED = "ROLLBACK_RECORDED"
    REPORT_CREATED = "REPORT_CREATED"


class CampaignState(StrEnum):
    BLOCKED = "BLOCKED"
    READY = "READY"
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class FirmValueCampaignCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class FirmValueCampaignPreflight:
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
    ready: bool
    checks: tuple[FirmValueCampaignCheck, ...]


@dataclass(frozen=True, slots=True)
class FirmValueCampaignEvent:
    sequence: int
    event_id: str
    previous_hash: str
    event_hash: str
    recorded_at: str
    kind: CampaignEventKind
    fixture: str | None
    strategy: str | None
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class FirmValueCampaignStatus:
    schema_version: str
    benchmark_id: str
    state: CampaignState
    manifest_content_hash: str
    completed_runs: int
    expected_runs: int
    failed_runs: int
    interrupted_runs: int
    next_fixture: str | None
    next_strategy: str | None
    max_model_calls_for_next_run: int
    max_wall_time_ms_for_next_run: int
    explicit_quota_confirmation_required: bool
    external_model_calls_recorded: int
    event_count: int
    ledger_verified: bool
    record_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FirmValueCampaignPreparation:
    preflight: FirmValueCampaignPreflight
    status: FirmValueCampaignStatus


@dataclass(frozen=True, slots=True)
class FirmValueCampaignRunResult:
    event: FirmValueCampaignEvent
    status: FirmValueCampaignStatus
    record_path: str | None
    task_success: bool


@dataclass(frozen=True, slots=True)
class FirmValueCampaignComparison:
    schema_version: str
    benchmark_id: str
    manifest_content_hash: str
    completed_runs: int
    expected_runs: int
    quality_gain_pair_count: int
    value_signal_pair_count: int
    dependency_attributed_value_count: int
    hard_safety_gate_passed: bool
    organization_gate_passed: bool
    no_validation_downgrade: bool
    manifest_budget_gate_passed: bool
    campaign_gate_passed: bool
    outcome: str
    recommended_direction: str
    aggregator_provider_calls: int
    aggregator_quota_consumed: bool
    aggregate_report: FirmValueReport


class FirmValueCampaignStore:
    def __init__(
        self,
        directory: str | Path,
        *,
        create: bool = False,
        db_name: str = "campaign.db",
        ledger_schema: str = CAMPAIGN_LEDGER_SCHEMA,
        event_id_prefix: str = "campaign-event",
    ) -> None:
        self.directory = Path(directory).expanduser().resolve()
        if "/" in db_name or "\\" in db_name or not db_name.endswith(".db"):
            raise ValueError("Campaign ledger filename is invalid")
        self.db_path = self.directory / db_name
        self.ledger_schema = ledger_schema
        self.event_id_prefix = event_id_prefix
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
        if not self.directory.is_dir() or self.directory.is_symlink():
            raise ValueError(f"Campaign directory is invalid: {self.directory}")
        if not create and (not self.db_path.is_file() or self.db_path.is_symlink()):
            raise ValueError(f"Campaign ledger is missing: {self.db_path}")
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if create:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS campaign_metadata(
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_events(
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    recorded_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    fixture TEXT,
                    strategy TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS campaign_run_start_unique
                    ON campaign_events(fixture, strategy)
                    WHERE kind = 'RUN_STARTED';
                CREATE UNIQUE INDEX IF NOT EXISTS campaign_run_terminal_unique
                    ON campaign_events(fixture, strategy)
                    WHERE kind IN ('RUN_RECORDED', 'RUN_FAILED', 'RUN_INTERRUPTED');
                """
            )
            self.connection.commit()
            os.chmod(self.db_path, 0o600)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> FirmValueCampaignStore:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def initialize(self, metadata: Mapping[str, object]) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute("SELECT COUNT(*) FROM campaign_metadata").fetchone()[0]
            if existing:
                raise ValueError("Campaign ledger is already initialized")
            for key, value in sorted(metadata.items()):
                self.connection.execute(
                    "INSERT INTO campaign_metadata(key, value_json) VALUES (?, ?)",
                    (key, _canonical(value)),
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def metadata(self) -> dict[str, object]:
        rows = self.connection.execute(
            "SELECT key, value_json FROM campaign_metadata ORDER BY key"
        ).fetchall()
        if not rows:
            raise ValueError("Campaign ledger metadata is missing")
        return {str(row["key"]): json.loads(row["value_json"]) for row in rows}

    def _event_hash(
        self,
        sequence: int,
        previous_hash: str,
        recorded_at: str,
        kind: CampaignEventKind,
        fixture: str | None,
        strategy: str | None,
        payload: Mapping[str, object],
    ) -> str:
        return content_digest(
            {
                "schema_version": self.ledger_schema,
                "sequence": sequence,
                "previous_hash": previous_hash,
                "recorded_at": recorded_at,
                "kind": kind.value,
                "fixture": fixture,
                "strategy": strategy,
                "payload": payload,
            }
        )

    def append(
        self,
        kind: CampaignEventKind,
        *,
        fixture: str | None = None,
        strategy: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> FirmValueCampaignEvent:
        safe_payload = dict(payload or {})
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT sequence, event_hash FROM campaign_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            sequence = (int(row["sequence"]) + 1) if row else 1
            previous_hash = str(row["event_hash"]) if row else "0" * 64
            recorded_at = utc_now().isoformat()
            event_hash = self._event_hash(
                sequence,
                previous_hash,
                recorded_at,
                kind,
                fixture,
                strategy,
                safe_payload,
            )
            event_id = f"{self.event_id_prefix}-{event_hash[:24]}"
            self.connection.execute(
                """
                INSERT INTO campaign_events(
                    sequence, event_id, previous_hash, event_hash, recorded_at,
                    kind, fixture, strategy, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    event_id,
                    previous_hash,
                    event_hash,
                    recorded_at,
                    kind.value,
                    fixture,
                    strategy,
                    _canonical(safe_payload),
                ),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return FirmValueCampaignEvent(
            sequence,
            event_id,
            previous_hash,
            event_hash,
            recorded_at,
            kind,
            fixture,
            strategy,
            safe_payload,
        )

    def events(self) -> tuple[FirmValueCampaignEvent, ...]:
        rows = self.connection.execute(
            "SELECT * FROM campaign_events ORDER BY sequence"
        ).fetchall()
        events: list[FirmValueCampaignEvent] = []
        previous = "0" * 64
        for expected_sequence, row in enumerate(rows, start=1):
            payload = json.loads(row["payload_json"])
            kind = CampaignEventKind(row["kind"])
            actual = self._event_hash(
                int(row["sequence"]),
                str(row["previous_hash"]),
                str(row["recorded_at"]),
                kind,
                row["fixture"],
                row["strategy"],
                payload,
            )
            if (
                int(row["sequence"]) != expected_sequence
                or str(row["previous_hash"]) != previous
                or str(row["event_hash"]) != actual
                or str(row["event_id"])
                != f"{self.event_id_prefix}-{actual[:24]}"
            ):
                raise ValueError("Campaign ledger hash chain is invalid")
            event = FirmValueCampaignEvent(
                sequence=expected_sequence,
                event_id=str(row["event_id"]),
                previous_hash=previous,
                event_hash=actual,
                recorded_at=str(row["recorded_at"]),
                kind=kind,
                fixture=str(row["fixture"]) if row["fixture"] is not None else None,
                strategy=str(row["strategy"]) if row["strategy"] is not None else None,
                payload=payload,
            )
            events.append(event)
            previous = actual
        return tuple(events)


def _load_preflight(path: Path) -> FirmValueCampaignPreflight:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Campaign preflight record is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    checks = tuple(FirmValueCampaignCheck(**item) for item in value["checks"])
    return FirmValueCampaignPreflight(
        **{key: value[key] for key in value if key != "checks"},
        checks=checks,
    )


def _campaign_artifacts(
    store: FirmValueCampaignStore,
) -> tuple[dict[str, object], Path, Path, FirmValueManifest, FirmValueCampaignPreflight]:
    metadata = store.metadata()
    manifest_path = store.directory / "manifest.json"
    preflight_path = store.directory / "preflight.json"
    if _sha256_file(manifest_path) != metadata.get("manifest_file_sha256"):
        raise ValueError("Campaign manifest file hash does not match the ledger")
    if _sha256_file(preflight_path) != metadata.get("preflight_file_sha256"):
        raise ValueError("Campaign preflight file hash does not match the ledger")
    manifest = load_firm_value_manifest(manifest_path)
    preflight = _load_preflight(preflight_path)
    if manifest.benchmark_id != metadata.get("benchmark_id"):
        raise ValueError("Campaign benchmark identity does not match the ledger")
    return metadata, manifest_path, preflight_path, manifest, preflight


def _process_is_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        return False
    return True


def campaign_status(directory: str | Path) -> FirmValueCampaignStatus:
    with FirmValueCampaignStore(directory) as store:
        metadata, _, _, manifest, preflight = _campaign_artifacts(store)
        events = store.events()
    slots = firm_value_expected_runs()
    recorded: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    failed: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    terminal_interrupted: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    started: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    for event in events:
        if event.fixture is None or event.strategy is None:
            continue
        key = (event.fixture, event.strategy)
        if key not in slots:
            raise ValueError("Campaign ledger contains an unknown run slot")
        if event.kind == CampaignEventKind.RUN_STARTED:
            if key in started or key in recorded or key in failed:
                raise ValueError("Campaign ledger reuses a run slot")
            started[key] = event
        elif event.kind == CampaignEventKind.RUN_RECORDED:
            if key not in started or key in recorded or key in failed:
                raise ValueError("Campaign recorded event has no unique start")
            recorded[key] = event
        elif event.kind in {CampaignEventKind.RUN_FAILED, CampaignEventKind.RUN_INTERRUPTED}:
            if (
                key not in started
                or key in recorded
                or key in failed
                or key in terminal_interrupted
            ):
                raise ValueError("Campaign failure event has no unique start")
            if event.kind == CampaignEventKind.RUN_INTERRUPTED:
                terminal_interrupted[key] = event
            else:
                failed[key] = event
    open_slots = {
        key: event
        for key, event in started.items()
        if key not in recorded and key not in failed and key not in terminal_interrupted
    }
    abandoned = sum(not _process_is_alive(event.payload.get("pid")) for event in open_slots.values())
    interrupted = len(terminal_interrupted) + abandoned
    running = len(open_slots) - abandoned
    if not preflight.ready:
        state = CampaignState.BLOCKED
    elif failed:
        state = CampaignState.PARTIAL_FAILED
    elif interrupted:
        state = CampaignState.INTERRUPTED
    elif running:
        state = CampaignState.RUNNING
    elif len(recorded) == len(slots):
        state = CampaignState.COMPLETE
    else:
        state = CampaignState.READY
    next_slot = None
    if state == CampaignState.READY:
        next_slot = next((slot for slot in slots if slot not in recorded), None)
    paths = tuple(
        str(store.directory / str(recorded[slot].payload["record_path"]))
        for slot in slots
        if slot in recorded
    )
    return FirmValueCampaignStatus(
        schema_version=CAMPAIGN_STATUS_SCHEMA,
        benchmark_id=manifest.benchmark_id,
        state=state,
        manifest_content_hash=manifest.content_hash,
        completed_runs=len(recorded),
        expected_runs=len(slots),
        failed_runs=len(failed),
        interrupted_runs=interrupted,
        next_fixture=next_slot[0] if next_slot else None,
        next_strategy=next_slot[1] if next_slot else None,
        max_model_calls_for_next_run=manifest.max_total_model_calls if next_slot else 0,
        max_wall_time_ms_for_next_run=manifest.max_wall_time_ms if next_slot else 0,
        explicit_quota_confirmation_required=next_slot is not None,
        external_model_calls_recorded=sum(
            int(event.payload.get("external_model_calls", 0)) for event in recorded.values()
        ),
        event_count=len(events),
        ledger_verified=True,
        record_paths=paths,
    )


async def prepare_firm_value_campaign(
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
) -> FirmValueCampaignPreparation:
    target = Path(directory).expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f"Campaign directory must not exist or must be empty: {target}")
    source = Path(source_root).expanduser().resolve()
    wheel_path = Path(wheel).expanduser().resolve()
    source_revision = source_snapshot_revision(source)
    distribution_sha256 = wheel_distribution_sha256(wheel_path)
    manifest = create_firm_value_manifest(
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
    offline = tuple(
        [
            await run_closed_loop_evaluation(fixture, strategy)
            for fixture, strategy in firm_value_expected_runs()
        ]
    )
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
    offline_passed = all(
        record.score.task_success and record.score.validation_passed for record in offline
    )
    checks = (
        FirmValueCampaignCheck("source-snapshot-frozen", True, source_revision),
        FirmValueCampaignCheck("wheel-hash-frozen", True, distribution_sha256),
        FirmValueCampaignCheck(
            "codex-executable-installed",
            bool(login.installed and login.executable and executable),
            executable or login.executable or command,
        ),
        FirmValueCampaignCheck(
            "codex-authenticated",
            bool(login.authenticated),
            "official login status passed" if login.authenticated else "authentication not confirmed",
        ),
        FirmValueCampaignCheck(
            "structured-output-cli-contract",
            structured_supported,
            capability_evidence,
        ),
        FirmValueCampaignCheck(
            "portable-first-party-validation",
            portable_commands,
            f"fixtures={len(manifest.fixtures)}",
        ),
        FirmValueCampaignCheck(
            "offline-six-run-rehearsal",
            offline_passed and len(offline) == 6,
            f"passed={sum(record.score.task_success for record in offline)}/{len(offline)}",
        ),
        FirmValueCampaignCheck(
            "one-run-quota-bound",
            4 <= max_total_model_calls <= 8
            and 1_000 <= max_wall_time_ms <= 600_000
            and request_timeout_seconds > 0,
            f"calls<={max_total_model_calls},wall_ms<={max_wall_time_ms}",
        ),
    )
    preflight = FirmValueCampaignPreflight(
        schema_version=CAMPAIGN_PREFLIGHT_SCHEMA,
        benchmark_id=manifest.benchmark_id,
        recorded_at=utc_now().isoformat(),
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        provider_kind=manifest.provider_kind,
        model_id=manifest.model_id,
        offline_runs_checked=len(offline),
        external_model_calls=0,
        quota_consumed=False,
        ready=all(check.passed for check in checks),
        checks=checks,
    )
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    manifest_path = _write_private(target / "manifest.json", firm_value_manifest_to_json(manifest))
    preflight_path = _write_private(
        target / "preflight.json",
        json.dumps(to_primitive(preflight), ensure_ascii=False, sort_keys=True, indent=2),
    )
    with FirmValueCampaignStore(target, create=True) as store:
        store.initialize(
            {
                "schema_version": CAMPAIGN_LEDGER_SCHEMA,
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
                "offline_runs_checked": len(offline),
                "external_model_calls": 0,
                "quota_consumed": False,
            },
        )
    return FirmValueCampaignPreparation(preflight, campaign_status(target))


def _verify_runtime_inputs(
    metadata: Mapping[str, object], manifest: FirmValueManifest
) -> None:
    source_root = Path(str(metadata["source_root"]))
    wheel_path = Path(str(metadata["wheel_path"]))
    if source_snapshot_revision(source_root) != manifest.source_revision:
        raise ValueError("Campaign source snapshot changed after preparation")
    if wheel_distribution_sha256(wheel_path) != manifest.distribution_sha256:
        raise ValueError("Campaign wheel changed after preparation")


async def run_next_campaign_slot(
    directory: str | Path,
    *,
    confirm_live_quota: bool,
    provider_factory=None,
    coding_worker_factory=None,
    live_runner: Callable[..., Awaitable[LiveCodingEvaluationRecord]] | None = None,
) -> FirmValueCampaignRunResult:
    status = campaign_status(directory)
    if status.state != CampaignState.READY or not status.next_fixture or not status.next_strategy:
        raise ValueError(f"Campaign cannot start a run while state is {status.state.value}")
    if not confirm_live_quota:
        raise ValueError(
            "Next campaign run requires --confirm-live-quota for exactly one slot: "
            f"{status.next_fixture}/{status.next_strategy}, "
            f"max_model_calls={status.max_model_calls_for_next_run}, "
            f"max_wall_time_ms={status.max_wall_time_ms_for_next_run}"
        )
    with FirmValueCampaignStore(directory) as store:
        metadata, manifest_path, _, manifest, _ = _campaign_artifacts(store)
        _verify_runtime_inputs(metadata, manifest)
        start = store.append(
            CampaignEventKind.RUN_STARTED,
            fixture=status.next_fixture,
            strategy=status.next_strategy,
            payload={
                "attempt": 1,
                "pid": os.getpid(),
                "quota_confirmed": True,
                "max_model_calls": manifest.max_total_model_calls,
                "max_wall_time_ms": manifest.max_wall_time_ms,
            },
        )
    config = LiveCodingEvaluationConfig(
        command=str(metadata["codex_command"]),
        model=manifest.model_id,
        timeout_seconds=float(metadata["request_timeout_seconds"]),
        source_revision=manifest.source_revision,
        max_total_model_calls=manifest.max_total_model_calls,
        max_wall_time_ms=manifest.max_wall_time_ms,
        quota_confirmed=True,
        company_revision=manifest.company_revision,
        roster_revision=manifest.roster_revision,
        playbook_revision=manifest.playbook_revision,
        distribution_sha256=manifest.distribution_sha256,
    )
    runner = live_runner or run_live_coding_evaluation
    try:
        record = await runner(
            config,
            status.next_fixture,
            status.next_strategy,
            provider_factory=provider_factory,
            coding_worker_factory=coding_worker_factory,
        )
        relative = Path("records") / (
            f"{start.sequence:02d}-{status.next_fixture}-{status.next_strategy}.json"
        )
        record_path = _write_private(
            Path(directory).expanduser().resolve() / relative,
            live_coding_record_to_json(record),
        )
        validate_firm_value_record(
            manifest_path,
            record_path,
            expected_fixture=status.next_fixture,
            expected_strategy=status.next_strategy,
        )
        with FirmValueCampaignStore(directory) as store:
            event = store.append(
                CampaignEventKind.RUN_RECORDED,
                fixture=status.next_fixture,
                strategy=status.next_strategy,
                payload={
                    "record_path": relative.as_posix(),
                    "record_file_sha256": _sha256_file(record_path),
                    "record_content_hash": record.content_hash,
                    "evaluation_run_id": record.evaluation_run_id,
                    "status": record.result.status.value,
                    "task_success": record.result.score.task_success,
                    "external_model_calls": record.external_model_calls,
                },
            )
        return FirmValueCampaignRunResult(
            event=event,
            status=campaign_status(directory),
            record_path=str(record_path),
            task_success=record.result.score.task_success,
        )
    except BaseException as exc:
        interrupted = isinstance(exc, (OperationCancelled, asyncio.CancelledError, KeyboardInterrupt))
        kind = CampaignEventKind.RUN_INTERRUPTED if interrupted else CampaignEventKind.RUN_FAILED
        relative = Path("failures") / (
            f"{start.sequence:02d}-{status.next_fixture}-{status.next_strategy}.json"
        )
        code = exc.code if isinstance(exc, ModelProviderError) else type(exc).__name__
        failure_payload = {
            "schema_version": "noruct.firm-value-campaign-failure.v1",
            "benchmark_id": status.benchmark_id,
            "fixture": status.next_fixture,
            "strategy": status.next_strategy,
            "recorded_at": utc_now().isoformat(),
            "failure_code": str(code),
            "interrupted": interrupted,
            "quota_confirmed": True,
            "partial_result_promoted": False,
        }
        failure_path = _write_private(
            Path(directory).expanduser().resolve() / relative,
            json.dumps(failure_payload, ensure_ascii=False, sort_keys=True, indent=2),
        )
        with FirmValueCampaignStore(directory) as store:
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
        return FirmValueCampaignRunResult(
            event=event,
            status=campaign_status(directory),
            record_path=None,
            task_success=False,
        )


def compare_campaign(
    directory: str | Path,
    *,
    output_path: str | Path | None = None,
) -> FirmValueCampaignComparison:
    status = campaign_status(directory)
    if status.state != CampaignState.COMPLETE:
        raise ValueError(
            f"Campaign comparison requires six sealed records; state={status.state.value}, "
            f"completed={status.completed_runs}/{status.expected_runs}"
        )
    root = Path(directory).expanduser().resolve()
    with FirmValueCampaignStore(root) as store:
        _, manifest_path, _, _, _ = _campaign_artifacts(store)
        events = store.events()
        recorded = {
            (event.fixture, event.strategy): event
            for event in events
            if event.kind == CampaignEventKind.RUN_RECORDED
        }
        record_paths = []
        for slot in firm_value_expected_runs():
            event = recorded[slot]
            path = root / str(event.payload["record_path"])
            if _sha256_file(path) != event.payload.get("record_file_sha256"):
                raise ValueError("Campaign record file changed after it was sealed")
            record_paths.append(path)
        report = aggregate_firm_value_records(manifest_path, record_paths)
        quality_gains = sum(
            pair.quality_delta >= QUALITY_GAIN_THRESHOLD for pair in report.pairs
        )
        value_signals = sum(pair.value_signal for pair in report.pairs)
        dependency_attributed = sum(
            pair.quality_delta >= QUALITY_GAIN_THRESHOLD
            and pair.value_signal
            and pair.organization_passed
            and (
                pair.dynamic_maximum_parallelism >= 2
                or pair.dynamic_task_attempt_count >= 2
            )
            for pair in report.pairs
        )
        campaign_gate = (
            report.hard_safety_gate_passed
            and report.organization_gate_passed
            and report.no_validation_downgrade
            and quality_gains >= 2
            and dependency_attributed >= 1
        )
        if not report.hard_safety_gate_passed:
            outcome = "SAFETY_GATE_FAILED"
            direction = "FREEZE_AND_FIX_SAFETY"
        elif not report.organization_gate_passed:
            outcome = "ORGANIZATION_GATE_FAILED"
            direction = "NARROW_TO_SOLO_FIRST"
        elif not report.no_validation_downgrade:
            outcome = "DYNAMIC_REGRESSION"
            direction = "NARROW_TO_SOLO_FIRST"
        elif campaign_gate:
            outcome = "DYNAMIC_VALUE_GATE_PASSED"
            direction = "OPEN_ONE_OBSERVED_BOTTLENECK"
        elif quality_gains >= 2 and dependency_attributed == 0:
            outcome = "ATTRIBUTION_INCONCLUSIVE"
            direction = "REPEAT_WITHOUT_FEATURE_EXPANSION"
        else:
            outcome = "DYNAMIC_VALUE_GATE_NOT_MET"
            direction = "FREEZE_ORGANIZATION_FEATURES"
        comparison = FirmValueCampaignComparison(
            schema_version=CAMPAIGN_COMPARISON_SCHEMA,
            benchmark_id=report.benchmark_id,
            manifest_content_hash=report.manifest_content_hash,
            completed_runs=6,
            expected_runs=6,
            quality_gain_pair_count=quality_gains,
            value_signal_pair_count=value_signals,
            dependency_attributed_value_count=dependency_attributed,
            hard_safety_gate_passed=report.hard_safety_gate_passed,
            organization_gate_passed=report.organization_gate_passed,
            no_validation_downgrade=report.no_validation_downgrade,
            manifest_budget_gate_passed=True,
            campaign_gate_passed=campaign_gate,
            outcome=outcome,
            recommended_direction=direction,
            aggregator_provider_calls=0,
            aggregator_quota_consumed=False,
            aggregate_report=report,
        )
        target = Path(output_path).expanduser().resolve() if output_path else root / "report.json"
        payload = json.dumps(to_primitive(comparison), ensure_ascii=False, sort_keys=True, indent=2)
        _write_private(target, payload)
        existing = [event for event in events if event.kind == CampaignEventKind.REPORT_CREATED]
        if not existing:
            store.append(
                CampaignEventKind.REPORT_CREATED,
                payload={
                    "report_path": str(target),
                    "report_file_sha256": _sha256_file(target),
                    "classification": comparison.outcome,
                    "aggregator_provider_calls": 0,
                    "aggregator_quota_consumed": False,
                },
            )
    return comparison
