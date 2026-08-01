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
    INFORMATION_BOUNDARY_LIVE_QUALITY_GAIN_THRESHOLD,
    INFORMATION_BOUNDARY_LIVE_STRATEGIES,
    InformationBoundaryArtifactProjection,
    InformationBoundaryCheck,
    LiveInformationBoundaryConfig,
    LiveInformationBoundaryRecord,
    information_boundary_benchmark_revision,
    information_boundary_fixture_revision,
    information_boundary_identity,
    information_boundary_memory_revision,
    live_information_boundary_record_to_json,
    load_information_boundary_preflight,
    load_live_information_boundary_record,
    run_live_information_boundary_evaluation,
)
INFORMATION_BOUNDARY_PAIR_MANIFEST_SCHEMA = (
    "noruct.information-boundary-pair-manifest.v5"
)
INFORMATION_BOUNDARY_PAIR_PREFLIGHT_SCHEMA = (
    "noruct.information-boundary-pair-preflight.v5"
)
INFORMATION_BOUNDARY_PAIR_STATUS_SCHEMA = (
    "noruct.information-boundary-pair-status.v5"
)
INFORMATION_BOUNDARY_PAIR_LEDGER_SCHEMA = (
    "noruct.information-boundary-pair-ledger.v5"
)
INFORMATION_BOUNDARY_PAIR_FAILURE_SCHEMA = (
    "noruct.information-boundary-pair-failure.v5"
)
INFORMATION_BOUNDARY_PAIR_COMPARISON_SCHEMA = (
    "noruct.information-boundary-pair-comparison.v5"
)
_PAIR_DB = "information-boundary-pair.db"
_FIXTURE_ID = "typed-information-boundary"


@dataclass(frozen=True, slots=True)
class InformationBoundaryPairExpectedRun:
    fixture: str
    strategy: str
    workload_hash: str
    run_id: str


@dataclass(frozen=True, slots=True)
class InformationBoundaryPairManifest:
    schema_version: str
    benchmark_id: str
    content_hash: str
    created_at: str
    expires_at: str
    noruct_version: str
    preflight_benchmark_id: str
    preflight_content_hash: str
    distribution_sha256: str
    source_revision: str
    provider_kind: str
    model_id: str
    authority_profile: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    memory_revision: str
    fixture_revision: str
    benchmark_revision: str
    max_model_calls_per_run: int
    max_model_calls_pair: int
    max_wall_time_ms_per_run: int
    quality_gain_threshold: float
    expected_runs: tuple[InformationBoundaryPairExpectedRun, ...]

    def content_payload(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "noruct_version": self.noruct_version,
            "preflight_benchmark_id": self.preflight_benchmark_id,
            "preflight_content_hash": self.preflight_content_hash,
            "distribution_sha256": self.distribution_sha256,
            "source_revision": self.source_revision,
            "provider_kind": self.provider_kind,
            "model_id": self.model_id,
            "authority_profile": self.authority_profile,
            "company_revision": self.company_revision,
            "roster_revision": self.roster_revision,
            "playbook_revision": self.playbook_revision,
            "memory_revision": self.memory_revision,
            "fixture_revision": self.fixture_revision,
            "benchmark_revision": self.benchmark_revision,
            "max_model_calls_per_run": self.max_model_calls_per_run,
            "max_model_calls_pair": self.max_model_calls_pair,
            "max_wall_time_ms_per_run": self.max_wall_time_ms_per_run,
            "quality_gain_threshold": self.quality_gain_threshold,
            "expected_runs": self.expected_runs,
        }


@dataclass(frozen=True, slots=True)
class InformationBoundaryPairPreflight:
    schema_version: str
    benchmark_id: str
    recorded_at: str
    phase44_benchmark_id: str
    phase44_content_hash: str
    provider_kind: str
    model_id: str
    external_model_calls: int
    quota_consumed: bool
    ready: bool
    checks: tuple[InformationBoundaryCheck, ...]


@dataclass(frozen=True, slots=True)
class InformationBoundaryPairStatus:
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
    external_model_calls_recorded: int
    event_count: int
    ledger_verified: bool
    record_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InformationBoundaryPairPreparation:
    preflight: InformationBoundaryPairPreflight
    status: InformationBoundaryPairStatus


@dataclass(frozen=True, slots=True)
class InformationBoundaryPairRunResult:
    event: FirmValueCampaignEvent
    status: InformationBoundaryPairStatus
    record_path: str | None
    task_success: bool


@dataclass(frozen=True, slots=True)
class InformationBoundaryPairComparison:
    schema_version: str
    benchmark_id: str
    manifest_content_hash: str
    completed_runs: int
    expected_runs: int
    artifact_quality_gain: float
    safety_gate_passed: bool
    organization_gate_passed: bool
    budget_gate_passed: bool
    pair_gate_passed: bool
    outcome: str
    recommended_direction: str
    checks: tuple[InformationBoundaryCheck, ...]
    aggregator_provider_calls: int
    aggregator_quota_consumed: bool


class InformationBoundaryPairStore(FirmValueCampaignStore):
    def __init__(self, directory: str | Path, *, create: bool = False) -> None:
        super().__init__(
            directory,
            create=create,
            db_name=_PAIR_DB,
            ledger_schema=INFORMATION_BOUNDARY_PAIR_LEDGER_SCHEMA,
            event_id_prefix="information-boundary-pair-event",
        )


def information_boundary_pair_expected_runs() -> tuple[tuple[str, str], ...]:
    return tuple((_FIXTURE_ID, strategy) for strategy in INFORMATION_BOUNDARY_LIVE_STRATEGIES)


def _create_manifest(
    phase44: Mapping[str, object],
    *,
    lifetime_hours: int,
    max_model_calls_per_run: int,
    max_model_calls_pair: int,
    max_wall_time_ms_per_run: int,
) -> InformationBoundaryPairManifest:
    if not 1 <= max_model_calls_per_run <= 6:
        raise ValueError("Information-boundary pair allows one to six calls per run")
    if (
        max_model_calls_pair != max_model_calls_per_run * 2
        or max_model_calls_pair > 12
    ):
        raise ValueError("Information-boundary pair call budget must equal two bounded runs")
    if (
        not 1_000 <= max_wall_time_ms_per_run <= 600_000
        or not 1 <= lifetime_hours <= 336
    ):
        raise ValueError("Information-boundary pair time bounds are invalid")
    created = utc_now().astimezone(timezone.utc)
    expires = created + timedelta(hours=lifetime_hours)
    model_id = str(phase44["reserved_model_profile"])
    revisions = (
        int(phase44["company_revision"]),
        int(phase44["roster_revision"]),
        int(phase44["playbook_revision"]),
    )
    identities = tuple(
        information_boundary_identity(
            strategy=strategy,
            model_profile=model_id,
            company_revision=revisions[0],
            roster_revision=revisions[1],
            playbook_revision=revisions[2],
            max_total_model_calls=max_model_calls_per_run,
            max_wall_time_ms=max_wall_time_ms_per_run,
        )
        for strategy in INFORMATION_BOUNDARY_LIVE_STRATEGIES
    )
    expected = tuple(
        InformationBoundaryPairExpectedRun(
            fixture=_FIXTURE_ID,
            strategy=identity.strategy,
            workload_hash=identity.workload_hash,
            run_id=identity.run_id,
        )
        for identity in identities
    )
    base = InformationBoundaryPairManifest(
        schema_version=INFORMATION_BOUNDARY_PAIR_MANIFEST_SCHEMA,
        benchmark_id="pending",
        content_hash="pending",
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        noruct_version=__version__,
        preflight_benchmark_id=str(phase44["benchmark_id"]),
        preflight_content_hash=str(phase44["content_hash"]),
        distribution_sha256=str(phase44["distribution_sha256"]),
        source_revision=str(phase44["source_revision"]),
        provider_kind="openai-codex-user-managed",
        model_id=model_id,
        authority_profile=str(phase44["authority_profile"]),
        company_revision=revisions[0],
        roster_revision=revisions[1],
        playbook_revision=revisions[2],
        memory_revision=str(phase44["memory_revision"]),
        fixture_revision=str(phase44["fixture_revision"]),
        benchmark_revision=str(phase44["benchmark_revision"]),
        max_model_calls_per_run=max_model_calls_per_run,
        max_model_calls_pair=max_model_calls_pair,
        max_wall_time_ms_per_run=max_wall_time_ms_per_run,
        quality_gain_threshold=INFORMATION_BOUNDARY_LIVE_QUALITY_GAIN_THRESHOLD,
        expected_runs=expected,
    )
    digest = content_digest(base.content_payload())
    return InformationBoundaryPairManifest(
        **{
            **to_primitive(base),
            "benchmark_id": f"information-boundary-pair-v5-{digest[:24]}",
            "content_hash": digest,
            "expected_runs": expected,
        }
    )


def _load_manifest(path: Path) -> InformationBoundaryPairManifest:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Information-boundary pair manifest is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != INFORMATION_BOUNDARY_PAIR_MANIFEST_SCHEMA
    ):
        raise ValueError("Information-boundary pair manifest schema is incompatible")
    expected = tuple(
        InformationBoundaryPairExpectedRun(**item) for item in value["expected_runs"]
    )
    manifest = InformationBoundaryPairManifest(
        **{
            **{key: item for key, item in value.items() if key != "expected_runs"},
            "expected_runs": expected,
        }
    )
    if (
        manifest.noruct_version != __version__
        or manifest.content_hash != content_digest(manifest.content_payload())
        or manifest.benchmark_id
        != f"information-boundary-pair-v5-{manifest.content_hash[:24]}"
        or tuple((item.fixture, item.strategy) for item in expected)
        != information_boundary_pair_expected_runs()
        or len({item.workload_hash for item in expected}) != 1
        or len({item.run_id for item in expected}) != 2
        or manifest.quality_gain_threshold
        != INFORMATION_BOUNDARY_LIVE_QUALITY_GAIN_THRESHOLD
        or manifest.max_model_calls_pair
        != manifest.max_model_calls_per_run * 2
        or manifest.max_model_calls_pair > 12
    ):
        raise ValueError("Information-boundary pair manifest contract is invalid")
    return manifest


def _load_pair_preflight(path: Path) -> InformationBoundaryPairPreflight:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Information-boundary pair preflight is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != INFORMATION_BOUNDARY_PAIR_PREFLIGHT_SCHEMA
    ):
        raise ValueError("Information-boundary pair preflight schema is incompatible")
    checks = tuple(InformationBoundaryCheck(**item) for item in value["checks"])
    return InformationBoundaryPairPreflight(
        **{key: item for key, item in value.items() if key != "checks"},
        checks=checks,
    )


def _campaign_artifacts(
    store: InformationBoundaryPairStore,
) -> tuple[
    dict[str, object],
    InformationBoundaryPairManifest,
    InformationBoundaryPairPreflight,
]:
    metadata = store.metadata()
    if metadata.get("schema_version") != INFORMATION_BOUNDARY_PAIR_LEDGER_SCHEMA:
        raise ValueError("Information-boundary pair ledger schema is invalid")
    manifest_path = store.directory / "manifest-v5.json"
    preflight_path = store.directory / "preflight-v5.json"
    phase44_path = store.directory / "phase44-preflight-v3.json"
    if _sha256_file(manifest_path) != metadata.get("manifest_file_sha256"):
        raise ValueError("Information-boundary pair manifest hash changed")
    if _sha256_file(preflight_path) != metadata.get("preflight_file_sha256"):
        raise ValueError("Information-boundary pair preflight hash changed")
    if _sha256_file(phase44_path) != metadata.get("phase44_file_sha256"):
        raise ValueError("Information-boundary Phase 44 preflight copy changed")
    manifest = _load_manifest(manifest_path)
    preflight = _load_pair_preflight(preflight_path)
    phase44 = load_information_boundary_preflight(phase44_path)
    if (
        manifest.benchmark_id != metadata.get("benchmark_id")
        or preflight.benchmark_id != manifest.benchmark_id
        or phase44["benchmark_id"] != manifest.preflight_benchmark_id
        or phase44["content_hash"] != manifest.preflight_content_hash
    ):
        raise ValueError("Information-boundary pair identity does not match its ledger")
    return metadata, manifest, preflight


def _manifest_fresh(manifest: InformationBoundaryPairManifest) -> bool:
    try:
        expires = datetime.fromisoformat(manifest.expires_at).astimezone(timezone.utc)
    except ValueError:
        return False
    return utc_now().astimezone(timezone.utc) <= expires


def _sealed_path(root: Path, relative: object, folder: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("Information-boundary pair sealed path is invalid")
    unresolved = root / relative
    candidate = unresolved.resolve()
    boundary = (root / folder).resolve()
    if (
        Path(relative).is_absolute()
        or candidate.parent != boundary
        or not candidate.is_file()
        or unresolved.is_symlink()
    ):
        raise ValueError("Information-boundary pair artifact escaped its sealed directory")
    return candidate


def _validate_failure(
    path: Path,
    manifest: InformationBoundaryPairManifest,
    *,
    expected_strategy: str,
) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Information-boundary pair failure cannot be read") from exc
    expected = next(
        item for item in manifest.expected_runs if item.strategy == expected_strategy
    )
    expected_keys = {
        "schema_version",
        "benchmark_id",
        "phase44_benchmark_id",
        "phase44_content_hash",
        "fixture",
        "strategy",
        "workload_hash",
        "evaluation_run_id",
        "recorded_at",
        "failure_code",
        "interrupted",
        "quota_confirmed",
        "partial_result_promoted",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("schema_version") != INFORMATION_BOUNDARY_PAIR_FAILURE_SCHEMA
        or value.get("benchmark_id") != manifest.benchmark_id
        or value.get("phase44_benchmark_id") != manifest.preflight_benchmark_id
        or value.get("phase44_content_hash") != manifest.preflight_content_hash
        or value.get("fixture") != _FIXTURE_ID
        or value.get("strategy") != expected_strategy
        or value.get("workload_hash") != expected.workload_hash
        or value.get("evaluation_run_id") != expected.run_id
        or value.get("quota_confirmed") is not True
        or value.get("partial_result_promoted") is not False
        or not isinstance(value.get("failure_code"), str)
        or not value["failure_code"]
    ):
        raise ValueError("Information-boundary pair failure contract is invalid")


def _validate_live_record(
    path: Path,
    manifest: InformationBoundaryPairManifest,
    *,
    expected_strategy: str,
) -> LiveInformationBoundaryRecord:
    record = load_live_information_boundary_record(path)
    expected = next(
        item for item in manifest.expected_runs if item.strategy == expected_strategy
    )
    if (
        record.evidence_class != INFORMATION_BOUNDARY_LIVE_EVIDENCE_CLASS
        or record.preflight_benchmark_id != manifest.preflight_benchmark_id
        or record.preflight_content_hash != manifest.preflight_content_hash
        or record.source_revision != manifest.source_revision
        or record.distribution_sha256 != manifest.distribution_sha256
        or record.model_id != manifest.model_id
        or record.authority_profile != manifest.authority_profile
        or record.company_revision != manifest.company_revision
        or record.roster_revision != manifest.roster_revision
        or record.playbook_revision != manifest.playbook_revision
        or record.memory_revision != manifest.memory_revision
        or record.fixture_revision != manifest.fixture_revision
        or record.benchmark_revision != manifest.benchmark_revision
        or record.strategy != expected_strategy
        or record.identity.workload_hash != expected.workload_hash
        or record.identity.run_id != expected.run_id
        or record.configured_model_call_limit != manifest.max_model_calls_per_run
        or record.configured_wall_time_ms != manifest.max_wall_time_ms_per_run
        or record.external_model_calls > manifest.max_model_calls_per_run
        or (record.task_success and not record.validation.passed)
    ):
        raise ValueError("Information-boundary live record violates the sealed manifest")
    return record


def _artifact_check(
    artifact: InformationBoundaryArtifactProjection,
    name: str,
) -> bool:
    return any(check.name == name and check.passed for check in artifact.checks)


