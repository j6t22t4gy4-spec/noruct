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


RELEASE_AUTHORIZATION_PAIR_MANIFEST_SCHEMA = (
    "noruct.release-authorization-pair-manifest.v6"
)
RELEASE_AUTHORIZATION_PAIR_PREFLIGHT_SCHEMA = (
    "noruct.release-authorization-pair-preflight.v5"
)
RELEASE_AUTHORIZATION_PAIR_STATUS_SCHEMA = (
    "noruct.release-authorization-pair-status.v6"
)
RELEASE_AUTHORIZATION_PAIR_LEDGER_SCHEMA = (
    "noruct.release-authorization-pair-ledger.v6"
)
RELEASE_AUTHORIZATION_PAIR_FAILURE_SCHEMA = (
    "noruct.release-authorization-pair-failure.v6"
)
RELEASE_AUTHORIZATION_PAIR_COMPARISON_SCHEMA = (
    "noruct.release-authorization-pair-comparison.v6"
)
_PAIR_DB = "release-authorization-pair.db"
_FIXTURE_ID = "release-authorization"
_SOLO_QUALITY_CEILING = 0.6


@dataclass(frozen=True, slots=True)
class ReleaseAuthorizationPairExpectedRun:
    fixture: str
    strategy: str
    workload_hash: str
    run_id: str


@dataclass(frozen=True, slots=True)
class ReleaseAuthorizationPairManifest:
    schema_version: str
    benchmark_id: str
    content_hash: str
    created_at: str
    expires_at: str
    noruct_version: str
    preflight_benchmark_id: str
    preflight_content_hash: str
    suite_revision: str
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
    solo_quality_ceiling: float
    quality_gain_threshold: float
    expected_runs: tuple[ReleaseAuthorizationPairExpectedRun, ...]

    def content_payload(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "noruct_version": self.noruct_version,
            "preflight_benchmark_id": self.preflight_benchmark_id,
            "preflight_content_hash": self.preflight_content_hash,
            "suite_revision": self.suite_revision,
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
            "solo_quality_ceiling": self.solo_quality_ceiling,
            "quality_gain_threshold": self.quality_gain_threshold,
            "expected_runs": self.expected_runs,
        }


@dataclass(frozen=True, slots=True)
class ReleaseAuthorizationPairPreflight:
    schema_version: str
    benchmark_id: str
    content_hash: str
    recorded_at: str
    noruct_version: str
    suite_revision: str
    suite_report_sha256: str
    source_revision: str
    distribution_sha256: str
    provider_kind: str
    model_id: str
    external_model_calls: int
    quota_consumed: bool
    ready: bool
    checks: tuple[InformationBoundaryCheck, ...]

    def content_payload(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "recorded_at": self.recorded_at,
            "noruct_version": self.noruct_version,
            "suite_revision": self.suite_revision,
            "suite_report_sha256": self.suite_report_sha256,
            "source_revision": self.source_revision,
            "distribution_sha256": self.distribution_sha256,
            "provider_kind": self.provider_kind,
            "model_id": self.model_id,
            "external_model_calls": self.external_model_calls,
            "quota_consumed": self.quota_consumed,
            "ready": self.ready,
            "checks": self.checks,
        }


@dataclass(frozen=True, slots=True)
class ReleaseAuthorizationPairStatus:
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
class ReleaseAuthorizationPairPreparation:
    preflight: ReleaseAuthorizationPairPreflight
    status: ReleaseAuthorizationPairStatus


@dataclass(frozen=True, slots=True)
class ReleaseAuthorizationPairRunResult:
    event: FirmValueCampaignEvent
    status: ReleaseAuthorizationPairStatus
    record_path: str | None
    task_success: bool


@dataclass(frozen=True, slots=True)
class ReleaseAuthorizationPairComparison:
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


class ReleaseAuthorizationPairStore(FirmValueCampaignStore):
    def __init__(self, directory: str | Path, *, create: bool = False) -> None:
        super().__init__(
            directory,
            create=create,
            db_name=_PAIR_DB,
            ledger_schema=RELEASE_AUTHORIZATION_PAIR_LEDGER_SCHEMA,
            event_id_prefix="release-authorization-pair-event",
        )


def release_authorization_pair_expected_runs() -> tuple[tuple[str, str], ...]:
    return tuple(
        (_FIXTURE_ID, strategy)
        for strategy in INFORMATION_BOUNDARY_LIVE_STRATEGIES
    )


def _create_preflight(
    *,
    recorded_at: str,
    suite_revision: str,
    suite_report_sha256: str,
    source_revision: str,
    distribution_sha256: str,
    model_id: str,
    ready: bool,
    checks: tuple[InformationBoundaryCheck, ...],
) -> ReleaseAuthorizationPairPreflight:
    base = ReleaseAuthorizationPairPreflight(
        schema_version=RELEASE_AUTHORIZATION_PAIR_PREFLIGHT_SCHEMA,
        benchmark_id="pending",
        content_hash="pending",
        recorded_at=recorded_at,
        noruct_version=__version__,
        suite_revision=suite_revision,
        suite_report_sha256=suite_report_sha256,
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        provider_kind="openai-codex-user-managed",
        model_id=model_id,
        external_model_calls=0,
        quota_consumed=False,
        ready=ready,
        checks=checks,
    )
    digest = content_digest(base.content_payload())
    return ReleaseAuthorizationPairPreflight(
        **{
            **to_primitive(base),
            "benchmark_id": f"release-authorization-preflight-v5-{digest[:24]}",
            "content_hash": digest,
            "checks": checks,
        }
    )


def _create_manifest(
    preflight: ReleaseAuthorizationPairPreflight,
    *,
    company_revision: int,
    roster_revision: int,
    playbook_revision: int,
    lifetime_hours: int,
    max_model_calls_per_run: int,
    max_model_calls_pair: int,
    max_wall_time_ms_per_run: int,
) -> ReleaseAuthorizationPairManifest:
    if not 1 <= max_model_calls_per_run <= 6:
        raise ValueError("Release-authorization pair allows one to six calls per run")
    if (
        max_model_calls_pair != max_model_calls_per_run * 2
        or max_model_calls_pair > 12
    ):
        raise ValueError(
            "Release-authorization pair call budget must equal two bounded runs"
        )
    if (
        not 1_000 <= max_wall_time_ms_per_run <= 600_000
        or not 1 <= lifetime_hours <= 336
    ):
        raise ValueError("Release-authorization pair time bounds are invalid")
    revisions = (company_revision, roster_revision, playbook_revision)
    if any(type(value) is not int or value < 0 for value in revisions):
        raise ValueError("Release-authorization revisions must be non-negative")
    created = utc_now().astimezone(timezone.utc)
    expires = created + timedelta(hours=lifetime_hours)
    identities = tuple(
        release_authorization_live_identity(
            strategy=strategy,
            model_profile=preflight.model_id,
            company_revision=company_revision,
            roster_revision=roster_revision,
            playbook_revision=playbook_revision,
            max_total_model_calls=max_model_calls_per_run,
            max_wall_time_ms=max_wall_time_ms_per_run,
        )
        for strategy in INFORMATION_BOUNDARY_LIVE_STRATEGIES
    )
    expected = tuple(
        ReleaseAuthorizationPairExpectedRun(
            fixture=_FIXTURE_ID,
            strategy=identity.strategy,
            workload_hash=identity.workload_hash,
            run_id=identity.run_id,
        )
        for identity in identities
    )
    base = ReleaseAuthorizationPairManifest(
        schema_version=RELEASE_AUTHORIZATION_PAIR_MANIFEST_SCHEMA,
        benchmark_id="pending",
        content_hash="pending",
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        noruct_version=__version__,
        preflight_benchmark_id=preflight.benchmark_id,
        preflight_content_hash=preflight.content_hash,
        suite_revision=preflight.suite_revision,
        distribution_sha256=preflight.distribution_sha256,
        source_revision=preflight.source_revision,
        provider_kind=preflight.provider_kind,
        model_id=preflight.model_id,
        authority_profile=INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
        memory_revision=release_authorization_memory_revision(),
        fixture_revision=release_authorization_fixture_revision(),
        benchmark_revision=release_authorization_benchmark_revision(),
        max_model_calls_per_run=max_model_calls_per_run,
        max_model_calls_pair=max_model_calls_pair,
        max_wall_time_ms_per_run=max_wall_time_ms_per_run,
        solo_quality_ceiling=_SOLO_QUALITY_CEILING,
        quality_gain_threshold=RELEASE_AUTHORIZATION_LIVE_QUALITY_GAIN_THRESHOLD,
        expected_runs=expected,
    )
    digest = content_digest(base.content_payload())
    return ReleaseAuthorizationPairManifest(
        **{
            **to_primitive(base),
            "benchmark_id": f"release-authorization-pair-v6-{digest[:24]}",
            "content_hash": digest,
            "expected_runs": expected,
        }
    )


def _load_manifest(path: Path) -> ReleaseAuthorizationPairManifest:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Release-authorization pair manifest is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version")
        != RELEASE_AUTHORIZATION_PAIR_MANIFEST_SCHEMA
    ):
        raise ValueError("Release-authorization pair manifest schema is incompatible")
    expected = tuple(
        ReleaseAuthorizationPairExpectedRun(**item)
        for item in value["expected_runs"]
    )
    manifest = ReleaseAuthorizationPairManifest(
        **{
            **{key: item for key, item in value.items() if key != "expected_runs"},
            "expected_runs": expected,
        }
    )
    if (
        manifest.noruct_version != __version__
        or manifest.content_hash != content_digest(manifest.content_payload())
        or manifest.benchmark_id
        != f"release-authorization-pair-v6-{manifest.content_hash[:24]}"
        or tuple((item.fixture, item.strategy) for item in expected)
        != release_authorization_pair_expected_runs()
        or len({item.workload_hash for item in expected}) != 1
        or len({item.run_id for item in expected}) != 2
        or manifest.solo_quality_ceiling != _SOLO_QUALITY_CEILING
        or manifest.quality_gain_threshold
        != RELEASE_AUTHORIZATION_LIVE_QUALITY_GAIN_THRESHOLD
        or manifest.max_model_calls_pair
        != manifest.max_model_calls_per_run * 2
        or manifest.max_model_calls_pair > 12
    ):
        raise ValueError("Release-authorization pair manifest contract is invalid")
    return manifest


def _load_preflight(path: Path) -> ReleaseAuthorizationPairPreflight:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Release-authorization pair preflight is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version")
        != RELEASE_AUTHORIZATION_PAIR_PREFLIGHT_SCHEMA
    ):
        raise ValueError("Release-authorization pair preflight schema is incompatible")
    checks = tuple(InformationBoundaryCheck(**item) for item in value["checks"])
    preflight = ReleaseAuthorizationPairPreflight(
        **{key: item for key, item in value.items() if key != "checks"},
        checks=checks,
    )
    if (
        preflight.noruct_version != __version__
        or preflight.content_hash != content_digest(preflight.content_payload())
        or preflight.benchmark_id
        != f"release-authorization-preflight-v5-{preflight.content_hash[:24]}"
        or preflight.external_model_calls != 0
        or preflight.quota_consumed
        or preflight.ready != all(check.passed for check in checks)
    ):
        raise ValueError("Release-authorization pair preflight contract is invalid")
    return preflight


def _campaign_artifacts(
    store: ReleaseAuthorizationPairStore,
) -> tuple[
    dict[str, object],
    ReleaseAuthorizationPairManifest,
    ReleaseAuthorizationPairPreflight,
    dict[str, object],
]:
    metadata = store.metadata()
    if metadata.get("schema_version") != RELEASE_AUTHORIZATION_PAIR_LEDGER_SCHEMA:
        raise ValueError("Release-authorization pair ledger schema is invalid")
    manifest_path = store.directory / "manifest-v6.json"
    preflight_path = store.directory / "preflight-v5.json"
    suite_path = store.directory / "suite-v4.json"
    if _sha256_file(manifest_path) != metadata.get("manifest_file_sha256"):
        raise ValueError("Release-authorization pair manifest hash changed")
    if _sha256_file(preflight_path) != metadata.get("preflight_file_sha256"):
        raise ValueError("Release-authorization pair preflight hash changed")
    if _sha256_file(suite_path) != metadata.get("suite_file_sha256"):
        raise ValueError("Release-authorization suite report hash changed")
    manifest = _load_manifest(manifest_path)
    preflight = _load_preflight(preflight_path)
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    if (
        not isinstance(suite, dict)
        or suite.get("schema_version") != INFORMATION_BOUNDARY_SUITE_REPORT_SCHEMA
        or suite.get("benchmark_revision") != manifest.suite_revision
        or suite.get("passed") is not True
        or suite.get("ready_for_second_live_control_pair") is not True
        or suite.get("external_provider_calls") != 0
        or suite.get("quota_consumed") is not False
        or manifest.benchmark_id != metadata.get("benchmark_id")
        or manifest.preflight_benchmark_id != preflight.benchmark_id
        or manifest.preflight_content_hash != preflight.content_hash
        or preflight.suite_report_sha256 != _sha256_file(suite_path)
    ):
        raise ValueError("Release-authorization pair identity does not match its ledger")
    return metadata, manifest, preflight, suite


def _manifest_fresh(manifest: ReleaseAuthorizationPairManifest) -> bool:
    try:
        expires = datetime.fromisoformat(manifest.expires_at).astimezone(timezone.utc)
    except ValueError:
        return False
    return utc_now().astimezone(timezone.utc) <= expires


def _sealed_path(root: Path, relative: object, folder: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("Release-authorization sealed path is invalid")
    unresolved = root / relative
    candidate = unresolved.resolve()
    boundary = (root / folder).resolve()
    if (
        Path(relative).is_absolute()
        or candidate.parent != boundary
        or not candidate.is_file()
        or unresolved.is_symlink()
    ):
        raise ValueError("Release-authorization artifact escaped its sealed directory")
    return candidate


def _validate_failure(
    path: Path,
    manifest: ReleaseAuthorizationPairManifest,
    *,
    expected_strategy: str,
) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Release-authorization pair failure cannot be read") from exc
    expected = next(
        item for item in manifest.expected_runs if item.strategy == expected_strategy
    )
    expected_keys = {
        "schema_version",
        "benchmark_id",
        "preflight_benchmark_id",
        "preflight_content_hash",
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
        or value.get("schema_version") != RELEASE_AUTHORIZATION_PAIR_FAILURE_SCHEMA
        or value.get("benchmark_id") != manifest.benchmark_id
        or value.get("preflight_benchmark_id") != manifest.preflight_benchmark_id
        or value.get("preflight_content_hash") != manifest.preflight_content_hash
        or value.get("fixture") != _FIXTURE_ID
        or value.get("strategy") != expected_strategy
        or value.get("workload_hash") != expected.workload_hash
        or value.get("evaluation_run_id") != expected.run_id
        or value.get("quota_confirmed") is not True
        or value.get("partial_result_promoted") is not False
        or not isinstance(value.get("failure_code"), str)
        or not value["failure_code"]
    ):
        raise ValueError("Release-authorization pair failure contract is invalid")


def _validate_live_record(
    path: Path,
    manifest: ReleaseAuthorizationPairManifest,
    *,
    expected_strategy: str,
) -> LiveReleaseAuthorizationRecord:
    record = load_live_release_authorization_record(path)
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
        raise ValueError("Release-authorization live record violates the manifest")
    return record


def _artifact_check(
    artifact: InformationBoundaryArtifactProjection,
    name: str,
) -> bool:
    return any(check.name == name and check.passed for check in artifact.checks)
