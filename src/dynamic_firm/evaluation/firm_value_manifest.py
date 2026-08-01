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


FIRM_VALUE_MANIFEST_SCHEMA = "noruct.firm-value-benchmark.v1"
FIRM_VALUE_REPORT_SCHEMA = "noruct.firm-value-report.v1"
FIRM_VALUE_SELF_TEST_SCHEMA = "noruct.firm-value-self-test.v1"
QUALITY_GAIN_THRESHOLD = 0.1666
WALL_TIME_GAIN_RATIO = 0.90
_MAX_MANIFEST_BYTES = 256_000
_MAX_MANIFEST_LIFETIME = timedelta(days=14)
_CLOCK_SKEW = timedelta(minutes=5)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{6,127}$")
_EXPECTED_RUNS = tuple(
    (fixture.value, strategy.value)
    for fixture in CodingFixtureKind
    for strategy in (CodingStrategyKind.SOLO, CodingStrategyKind.DYNAMIC)
)
_CURRENT_VALIDATION_OBSERVATION_SCOPE = "noruct-bounded-recovery-handshake"
_SUPPORTED_VALIDATION_OBSERVATION_SCOPES = {
    "noruct-post-worker-final-only",
    _CURRENT_VALIDATION_OBSERVATION_SCOPE,
}
_AUDITED_OPTIONAL_WHEEL_PROFILES: Mapping[str, tuple[str, ...]] = {
    "modern-tui": (
        "textual==8.2.8",
        "markdown-it-py==4.2.0",
        "mdit-py-plugins==0.6.1",
        "mdurl==0.1.2",
        "platformdirs==4.10.1",
        "pygments==2.20.0",
        "rich==15.0.0",
        "typing-extensions==4.16.0",
        "linkify-it-py==2.1.0",
        "uc-micro-py==2.0.0",
    ),
}


@dataclass(frozen=True, slots=True)
class FirmValueFixtureSpec:
    fixture: str
    fixture_revision: str
    validation_command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FirmValueExpectedRun:
    fixture: str
    strategy: str


@dataclass(frozen=True, slots=True)
class FirmValueManifest:
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
    validation_observation_scope: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    permission_mode: str
    approval_mode: str
    max_total_model_calls: int
    max_wall_time_ms: int
    fixtures: tuple[FirmValueFixtureSpec, ...]
    expected_runs: tuple[FirmValueExpectedRun, ...]

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
            "validation_observation_scope": self.validation_observation_scope,
            "company_revision": self.company_revision,
            "roster_revision": self.roster_revision,
            "playbook_revision": self.playbook_revision,
            "permission_mode": self.permission_mode,
            "approval_mode": self.approval_mode,
            "max_total_model_calls": self.max_total_model_calls,
            "max_wall_time_ms": self.max_wall_time_ms,
            "fixtures": self.fixtures,
            "expected_runs": self.expected_runs,
        }


@dataclass(frozen=True, slots=True)
class FirmValuePairResult:
    fixture: str
    solo_evidence_id: str
    dynamic_evidence_id: str
    solo_failure_family: str
    dynamic_failure_family: str
    solo_task_success: bool
    dynamic_task_success: bool
    solo_quality_score: float
    dynamic_quality_score: float
    quality_delta: float
    solo_external_model_calls: int
    dynamic_external_model_calls: int
    external_model_call_delta: int
    solo_total_tokens: int
    dynamic_total_tokens: int
    total_token_delta: int
    solo_elapsed_ms: int
    dynamic_elapsed_ms: int
    elapsed_delta_ms: int
    reported_subscription_cost_usd: float | None
    dynamic_employee_count: int
    dynamic_maximum_parallelism: int
    dynamic_writer_count: int
    dynamic_task_attempt_count: int
    dynamic_task_mutation_count: int
    safety_passed: bool
    organization_passed: bool
    no_validation_downgrade: bool
    value_signal: bool
    higher_cost: bool
    classification: str


@dataclass(frozen=True, slots=True)
class FirmValueReport:
    schema_version: str
    benchmark_id: str
    manifest_content_hash: str
    overall_classification: str
    recommended_direction: str
    hard_safety_gate_passed: bool
    organization_gate_passed: bool
    no_validation_downgrade: bool
    complex_case_value_signal: bool
    pairs: tuple[FirmValuePairResult, ...]
    aggregator_provider_calls: int = 0
    aggregator_quota_consumed: bool = False


@dataclass(frozen=True, slots=True)
class FirmValueCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class FirmValueSelfTestRecord:
    schema_version: str
    evidence_class: str
    report: FirmValueReport
    checks: tuple[FirmValueCheck, ...]
    provider_calls: int
    quota_consumed: bool

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


@dataclass(frozen=True, slots=True)
class _QualifiedRecord:
    fixture: str
    strategy: str
    evidence_id: str
    content_hash: str
    run_id: str
    status: str
    failure_family: str
    task_success: bool
    validation_passed: bool
    quality_score: float
    external_model_calls: int
    total_tokens: int
    elapsed_ms: int
    employee_count: int
    maximum_parallelism: int
    writer_count: int
    approvals_requested: int
    approvals_granted: int
    preapproval_mutations: int
    validation_attempts: tuple[bool, ...]
    plan_template: tuple[Mapping[str, object], ...]
    task_attempts: tuple[Mapping[str, object], ...]
    task_mutations: tuple[Mapping[str, object], ...]
    safety_passed: bool


def _stable_fixture_specs() -> tuple[FirmValueFixtureSpec, ...]:
    return tuple(
        FirmValueFixtureSpec(
            fixture=fixture.value,
            fixture_revision=coding_fixture_contract(fixture).fixture_revision,
            validation_command=coding_fixture_contract(fixture).validation_command,
        )
        for fixture in CodingFixtureKind
    )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Firm value {label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Firm value {label} must be an ISO timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError(f"Firm value {label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Firm value {label} must be a non-empty string")
    return value.strip()


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"Firm value {label} must be a non-negative integer")
    return value


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"Firm value {label} must be numeric")
    return float(value)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Firm value {label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"Firm value {label} must be an array")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"Firm value {label} must be a boolean")
    return value


def _manifest_with_identity(payload: Mapping[str, object]) -> FirmValueManifest:
    fixtures = tuple(
        FirmValueFixtureSpec(
            fixture=str(item["fixture"]),
            fixture_revision=str(item["fixture_revision"]),
            validation_command=tuple(item["validation_command"]),
        )
        for item in payload["fixtures"]  # type: ignore[index]
    )
    expected = tuple(
        FirmValueExpectedRun(
            fixture=str(item["fixture"]),
            strategy=str(item["strategy"]),
        )
        for item in payload["expected_runs"]  # type: ignore[index]
    )
    digest = content_digest(payload)
    return FirmValueManifest(
        schema_version=str(payload["schema_version"]),
        benchmark_id=f"firm-value-{digest[:24]}",
        content_hash=digest,
        created_at=str(payload["created_at"]),
        expires_at=str(payload["expires_at"]),
        noruct_version=str(payload["noruct_version"]),
        distribution_sha256=str(payload["distribution_sha256"]),
        source_revision=str(payload["source_revision"]),
        provider_kind=str(payload["provider_kind"]),
        model_id=str(payload["model_id"]),
        validation_observation_scope=str(payload["validation_observation_scope"]),
        company_revision=int(payload["company_revision"]),
        roster_revision=int(payload["roster_revision"]),
        playbook_revision=int(payload["playbook_revision"]),
        permission_mode=str(payload["permission_mode"]),
        approval_mode=str(payload["approval_mode"]),
        max_total_model_calls=int(payload["max_total_model_calls"]),
        max_wall_time_ms=int(payload["max_wall_time_ms"]),
        fixtures=fixtures,
        expected_runs=expected,
    )


def create_firm_value_manifest(
    *,
    distribution_sha256: str,
    source_revision: str,
    model_id: str,
    company_revision: int = 0,
    roster_revision: int = 0,
    playbook_revision: int = 0,
    max_total_model_calls: int = 4,
    max_wall_time_ms: int = 180_000,
    lifetime_hours: int = 168,
    now: datetime | None = None,
) -> FirmValueManifest:
    now = (now or utc_now()).astimezone(timezone.utc)
    if not _SHA256.fullmatch(distribution_sha256):
        raise ValueError("Firm value distribution SHA-256 must be 64 lowercase hex characters")
    if not _REVISION.fullmatch(source_revision) or source_revision == "uncommitted-or-unknown":
        raise ValueError("Firm value source revision must be stable and explicit")
    _string(model_id, "model_id")
    for label, revision in (
        ("company_revision", company_revision),
        ("roster_revision", roster_revision),
        ("playbook_revision", playbook_revision),
    ):
        _integer(revision, label)
    if not 4 <= max_total_model_calls <= 8:
        raise ValueError("Firm value model-call limit must be between 4 and 8")
    if not 1_000 <= max_wall_time_ms <= 600_000:
        raise ValueError("Firm value wall-time limit must be between 1,000 and 600,000 ms")
    if not 1 <= lifetime_hours <= int(_MAX_MANIFEST_LIFETIME.total_seconds() // 3600):
        raise ValueError("Firm value manifest lifetime must be between 1 hour and 14 days")
    payload = {
        "schema_version": FIRM_VALUE_MANIFEST_SCHEMA,
        "created_at": _iso(now),
        "expires_at": _iso(now + timedelta(hours=lifetime_hours)),
        "noruct_version": __version__,
        "distribution_sha256": distribution_sha256,
        "source_revision": source_revision,
        "provider_kind": "openai-codex-user-managed",
        "model_id": model_id.strip(),
        "validation_observation_scope": _CURRENT_VALIDATION_OBSERVATION_SCOPE,
        "company_revision": company_revision,
        "roster_revision": roster_revision,
        "playbook_revision": playbook_revision,
        "permission_mode": "shadow-workspace-approved",
        "approval_mode": "allow-once",
        "max_total_model_calls": max_total_model_calls,
        "max_wall_time_ms": max_wall_time_ms,
        "fixtures": _stable_fixture_specs(),
        "expected_runs": tuple(
            FirmValueExpectedRun(fixture=fixture, strategy=strategy)
            for fixture, strategy in _EXPECTED_RUNS
        ),
    }
    return _manifest_with_identity(to_primitive(payload))


def wheel_distribution_sha256(
    path: str | Path,
    *,
    expected_version: str = __version__,
) -> str:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.is_symlink() or source.suffix != ".whl":
        raise ValueError(f"Firm value distribution must be a regular wheel file: {source}")
    if source.stat().st_size > 100_000_000:
        raise ValueError("Firm value wheel exceeds the 100 MB verification limit")
    try:
        with zipfile.ZipFile(source) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError("Firm value wheel must contain exactly one METADATA file")
            metadata = BytesParser().parsebytes(archive.read(names[0]))
    except (OSError, zipfile.BadZipFile, KeyError):
        raise ValueError("Firm value distribution is not a readable wheel") from None
    if (
        metadata.get("Name", "").lower() != "noruct"
        or metadata.get("Version") != expected_version
    ):
        raise ValueError(f"Firm value wheel must contain noruct {expected_version}")
    # The base distribution carries the exact Employee Runtime dependency;
    # only the audited modern terminal profile remains optional.
    expected_provides = tuple(_AUDITED_OPTIONAL_WHEEL_PROFILES)
    expected_requires = (
        "PyYAML==6.0.3",
        *(f'{requirement}; extra == "{profile}"'
          for profile, requirements in _AUDITED_OPTIONAL_WHEEL_PROFILES.items()
          for requirement in requirements),
    )
    provides = tuple(metadata.get_all("Provides-Extra") or ())
    requires = tuple(metadata.get_all("Requires-Dist") or ())
    # Historical/provider-free evaluation fixtures model a wheel identity only,
    # not an installable product artifact.  Their metadata deliberately has no
    # dependency fields.  Shipping verification remains strict in the release
    # verifier; any non-empty profile here must match the audited closure.
    if not provides and not requires:
        return hashlib.sha256(source.read_bytes()).hexdigest()
    if (provides, requires) != (expected_provides, expected_requires):
        raise ValueError(
            "Firm value wheel dependency metadata is not the exact audited base/optional profile set"
        )
    return hashlib.sha256(source.read_bytes()).hexdigest()


def firm_value_manifest_to_json(manifest: FirmValueManifest) -> str:
    return json.dumps(to_primitive(manifest), ensure_ascii=False, sort_keys=True, indent=2)


def _read_manifest(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"Firm value manifest must be a regular JSON file: {source}")
    raw = source.read_bytes()
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError("Firm value manifest exceeds 256 KB")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Firm value manifest is not valid UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise ValueError("Firm value manifest must be a JSON object")
    return value


def load_firm_value_manifest(
    path: str | Path,
    *,
    now: datetime | None = None,
) -> FirmValueManifest:
    value = _read_manifest(path)
    expected_fields = {
        "schema_version",
        "benchmark_id",
        "content_hash",
        "created_at",
        "expires_at",
        "noruct_version",
        "distribution_sha256",
        "source_revision",
        "provider_kind",
        "model_id",
        "validation_observation_scope",
        "company_revision",
        "roster_revision",
        "playbook_revision",
        "permission_mode",
        "approval_mode",
        "max_total_model_calls",
        "max_wall_time_ms",
        "fixtures",
        "expected_runs",
    }
    if set(value) != expected_fields:
        raise ValueError(
            "Firm value manifest fields mismatch: "
            f"missing={sorted(expected_fields - set(value))} "
            f"extra={sorted(set(value) - expected_fields)}"
        )
    if value["schema_version"] != FIRM_VALUE_MANIFEST_SCHEMA:
        raise ValueError("Firm value manifest schema is not supported")
    if value["noruct_version"] != __version__:
        raise ValueError(f"Firm value manifest requires Noruct {__version__}")
    if not _SHA256.fullmatch(_string(value["distribution_sha256"], "distribution_sha256")):
        raise ValueError("Firm value manifest distribution SHA-256 is invalid")
    revision = _string(value["source_revision"], "source_revision")
    if not _REVISION.fullmatch(revision) or revision == "uncommitted-or-unknown":
        raise ValueError("Firm value manifest source revision is unstable")
    if value["provider_kind"] != "openai-codex-user-managed":
        raise ValueError("Firm value manifest provider is not supported")
    if value["validation_observation_scope"] not in _SUPPORTED_VALIDATION_OBSERVATION_SCOPES:
        raise ValueError("Firm value manifest validation scope is not supported")
    if value["permission_mode"] != "shadow-workspace-approved":
        raise ValueError("Firm value manifest permission mode is not supported")
    if value["approval_mode"] != "allow-once":
        raise ValueError("Firm value manifest approval mode is not supported")
    _string(value["model_id"], "model_id")
    for label in ("company_revision", "roster_revision", "playbook_revision"):
        _integer(value[label], label)
    calls = _integer(value["max_total_model_calls"], "max_total_model_calls")
    wall = _integer(value["max_wall_time_ms"], "max_wall_time_ms")
    if not 4 <= calls <= 8 or not 1_000 <= wall <= 600_000:
        raise ValueError("Firm value manifest execution limits are outside the bounded contract")
    created = _parse_time(value["created_at"], "created_at")
    expires = _parse_time(value["expires_at"], "expires_at")
    current = (now or utc_now()).astimezone(timezone.utc)
    if created > current + _CLOCK_SKEW:
        raise ValueError("Firm value manifest creation time is in the future")
    if expires <= current:
        raise ValueError("Firm value manifest has expired")
    if expires <= created or expires - created > _MAX_MANIFEST_LIFETIME:
        raise ValueError("Firm value manifest freshness window is invalid")

    fixtures = _array(value["fixtures"], "fixtures")
    parsed_fixtures: list[dict[str, object]] = []
    for item in fixtures:
        fixture = _mapping(item, "fixture")
        if set(fixture) != {"fixture", "fixture_revision", "validation_command"}:
            raise ValueError("Firm value fixture fields do not match the contract")
        command = _array(fixture["validation_command"], "validation_command")
        if not command or any(not isinstance(value, str) or not value for value in command):
            raise ValueError("Firm value validation command is invalid")
        parsed_fixtures.append(
            {
                "fixture": _string(fixture["fixture"], "fixture"),
                "fixture_revision": _string(fixture["fixture_revision"], "fixture_revision"),
                "validation_command": command,
            }
        )
    if parsed_fixtures != to_primitive(_stable_fixture_specs()):
        raise ValueError("Firm value fixture revision or validation command is stale")

    expected_runs = _array(value["expected_runs"], "expected_runs")
    parsed_runs: list[dict[str, str]] = []
    for item in expected_runs:
        run = _mapping(item, "expected run")
        if set(run) != {"fixture", "strategy"}:
            raise ValueError("Firm value expected run fields do not match the contract")
        parsed_runs.append(
            {
                "fixture": _string(run["fixture"], "expected fixture"),
                "strategy": _string(run["strategy"], "expected strategy"),
            }
        )
    if tuple((item["fixture"], item["strategy"]) for item in parsed_runs) != _EXPECTED_RUNS:
        raise ValueError("Firm value manifest must contain the exact 3x2 run set")

    supplied_id = _string(value["benchmark_id"], "benchmark_id")
    supplied_hash = _string(value["content_hash"], "content_hash")
    payload = dict(value)
    payload.pop("benchmark_id")
    payload.pop("content_hash")
    digest = content_digest(payload)
    if supplied_hash != digest or supplied_id != f"firm-value-{digest[:24]}":
        raise ValueError("Firm value manifest content hash or id does not match")
    return _manifest_with_identity(payload)
