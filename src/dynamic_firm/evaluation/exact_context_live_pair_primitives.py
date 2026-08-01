from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from dynamic_firm import __version__
from dynamic_firm.company.models import content_digest
from dynamic_firm.compiler import (
    CapabilityInsertReplanner,
    CompilerExecutionProfile,
    CompilerRequest,
    WorkflowPrior,
    solo_first_decision,
)
from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    EmployeeRecord,
    JobLimits,
    JobResult,
    JobStatus,
)
from dynamic_firm.kernel.service import FirmKernel
from dynamic_firm.kernel.testing import ScriptedEmployeeExecutionPort, ScriptedOutcome
from dynamic_firm.providers.codex_exec import (
    CodexExecProvider,
    CodexExecProviderConfig,
    CodexLoginStatus,
)
from dynamic_firm.runtime.models import (
    ActionPolicy,
    CompletionEnvelope,
    CompletionValidation,
    ContextBundle,
    EmployeeRunRequest,
    EventType,
    RunEvent,
    RunLimits,
    RunStatus,
    SignalCode,
    VersionedContent,
    to_primitive,
    utc_now,
)
from dynamic_firm.runtime.ports import ModelProviderError, OperationCancelled
from dynamic_firm.runtime.prompt import PromptBuilder
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry

from .alpha_readiness import AlphaReadinessEvaluation, run_alpha_readiness_evaluation
from .context_binding import (
    ExactContextBoundExpectedRun,
    ExactContextBoundPreparation,
    ExactContextEvidenceBinding,
    load_exact_context_bound_preparation,
    load_exact_context_evidence_binding,
)
from .eval_contracts import EvaluationTrajectoryProjection, project_job_trajectory
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
    InformationBoundaryAdmissionProjection,
    InformationBoundaryArtifactProjection,
    InformationBoundaryCheck,
    InformationBoundaryCostProjection,
    InformationBoundarySafetyProjection,
    _RecordingEmployeeExecutionPort,
    _provider_request_refs,
)
from .workflow_patch_efficiency import (
    WORKFLOW_PATCH_NATURAL_GOAL,
    _parent_seed,
)
from .workflow_patch_live import workflow_patch_candidate_prior


EXACT_CONTEXT_LIVE_PAIR_MANIFEST_SCHEMA = (
    "noruct.exact-context-source-frozen-live-pair-manifest.v1"
)
EXACT_CONTEXT_LIVE_PAIR_PREFLIGHT_SCHEMA = (
    "noruct.exact-context-source-frozen-live-pair-preflight.v1"
)
EXACT_CONTEXT_LIVE_PAIR_RECORD_SCHEMA = (
    "noruct.exact-context-source-frozen-live-record.v1"
)
EXACT_CONTEXT_LIVE_PAIR_FAILURE_SCHEMA = (
    "noruct.exact-context-source-frozen-live-failure.v1"
)
EXACT_CONTEXT_LIVE_PAIR_LEDGER_SCHEMA = (
    "noruct.exact-context-source-frozen-live-ledger.v1"
)
EXACT_CONTEXT_LIVE_PAIR_COMPARISON_SCHEMA = (
    "noruct.exact-context-source-frozen-live-comparison.v1"
)
EXACT_CONTEXT_NATURAL_EVIDENCE_SCHEMA = (
    "noruct.exact-context-natural-evidence-projection.v1"
)
EXACT_CONTEXT_LIVE_EVIDENCE_CLASS = "LIVE_EVALUATION"
EXACT_CONTEXT_COMPLETION_CONTRACT_REVISION = (
    "exact-context-alpha-readiness-task-objective-v1"
)
EXACT_CONTEXT_COMPLETION_VALIDATOR_REVISION = (
    "exact-context-alpha-readiness-validator-v1"
)
EXACT_CONTEXT_PROJECTION_REVISION = "exact-context-alpha-readiness-projection-v1"
EXACT_CONTEXT_LIVE_STRATEGIES = (
    "exact-context-control",
    "exact-context-candidate",
)
EXACT_CONTEXT_QUALITY_GAIN_THRESHOLD = 0.2
_PAIR_DB = "exact-context-live-pair.db"
_RECORD_MAX_BYTES = 1_000_000
_BLOCKERS = (
    "operator-release-approval",
    "alpha-version-staged",
    "clean-release-worktree",
)
_BLOCKER_VALUE = ",".join(_BLOCKERS)
_REVIEW_BASIS = "source-frozen-gates-consistent"
_MISSING_REVIEW = "unavailable"
_FIELD_LINE = re.compile(
    r"^\s*([a-z][a-z0-9_]*)\s*([=:])\s*([^\r\n]+?)\s*$"
)


from .exact_context_live_pair_contracts import (
    ExactContextLivePairComparison,
    ExactContextLivePairManifest,
    ExactContextLivePairPreparation,
    ExactContextLivePairPreflight,
    ExactContextLivePairRunResult,
    ExactContextLivePairState,
    ExactContextLivePairStatus,
    ExactContextLiveRecord,
    ExactContextNaturalEvidence,
    ExactContextRegressionProbe,
    ExactContextValidationProjection,
)

class ExactContextLivePairStore(FirmValueCampaignStore):
    def __init__(self, directory: str | Path, *, create: bool = False) -> None:
        super().__init__(
            directory,
            create=create,
            db_name=_PAIR_DB,
            ledger_schema=EXACT_CONTEXT_LIVE_PAIR_LEDGER_SCHEMA,
            event_id_prefix="exact-context-live-pair-event",
        )


def _canonical_json(value: object) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


def _summary_fields(summary: str) -> tuple[dict[str, str], tuple[str, ...]]:
    fields: dict[str, str] = {}
    conflicts: list[str] = []
    for line in summary.splitlines():
        match = _FIELD_LINE.fullmatch(line)
        if match is None:
            continue
        key = match.group(1)
        value = match.group(3).strip().strip("`")
        existing = fields.get(key)
        if existing is not None and existing != value:
            conflicts.append(key)
            continue
        fields[key] = value
    return fields, tuple(dict.fromkeys(conflicts))


def _manifest_fresh(manifest: ExactContextLivePairManifest) -> bool:
    try:
        expires = datetime.fromisoformat(manifest.expires_at).astimezone(timezone.utc)
    except ValueError:
        return False
    return utc_now().astimezone(timezone.utc) <= expires


def _run_limits(manifest: ExactContextLivePairManifest) -> RunLimits:
    return RunLimits(
        max_model_calls=manifest.max_model_calls_per_run,
        max_tool_calls=4,
        max_input_tokens=manifest.max_input_tokens_per_run,
        max_output_tokens=manifest.max_output_tokens_per_run,
        max_cost_usd=manifest.max_cost_usd_per_run,
        max_wall_time_ms=manifest.max_wall_time_ms_per_run,
    )


def _job_limits(manifest: ExactContextLivePairManifest) -> JobLimits:
    return JobLimits(
        max_tasks=4,
        max_concurrency=1,
        max_graph_patches=1,
        max_task_mutations=1,
        max_temporary_roles=2,
        max_total_model_calls=manifest.max_model_calls_per_run,
        max_total_tool_calls=4,
        max_total_cost_usd=manifest.max_cost_usd_per_run,
        max_wall_time_ms=manifest.max_wall_time_ms_per_run,
    )


def run_python311_regression_probe(
    source_root: str | Path,
    *,
    python_command: str = sys.executable,
    timeout_seconds: float = 180.0,
) -> ExactContextRegressionProbe:
    """Run the bounded first-party suite and retain only aggregate evidence."""

    source = Path(source_root).expanduser().resolve()
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Exact-context regression source root is invalid")
    executable = Path(python_command).expanduser().resolve()
    if not executable.is_file() or executable.is_symlink():
        raise ValueError("Exact-context regression Python must be an absolute file")
    version = subprocess.run(
        [str(executable), "--version"],
        cwd=source,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
    )
    version_text = version.stdout[:256].decode("utf-8", errors="replace").strip()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "SYSTEMROOT"}
    }
    environment["PYTHONPATH"] = str(source / "src")
    completed = subprocess.run(
        [
            str(executable),
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_*.py",
        ],
        cwd=source,
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
    )
    output = completed.stdout[:512_000]
    decoded = output.decode("utf-8", errors="replace")
    count_match = re.search(r"Ran\s+(\d+)\s+tests?", decoded)
    skipped_match = re.search(r"skipped=(\d+)", decoded)
    test_count = int(count_match.group(1)) if count_match else 0
    skipped_count = int(skipped_match.group(1)) if skipped_match else 0
    passed = (
        version.returncode == 0
        and version_text.startswith("Python 3.11.")
        and completed.returncode == 0
        and test_count > 0
        and "OK" in decoded
    )
    return ExactContextRegressionProbe(
        python_version=version_text,
        passed=passed,
        test_count=test_count,
        skipped_count=skipped_count,
        return_code=completed.returncode,
        output_sha256=hashlib.sha256(output).hexdigest(),
    )


def _create_natural_evidence(
    *,
    source_revision: str,
    distribution_sha256: str,
    regression: ExactContextRegressionProbe,
    alpha: AlphaReadinessEvaluation,
    alpha_report_sha256: str,
) -> ExactContextNaturalEvidence:
    passed_checks = sum(check.passed for check in alpha.checks)
    base = ExactContextNaturalEvidence(
        schema_version=EXACT_CONTEXT_NATURAL_EVIDENCE_SCHEMA,
        content_hash="pending",
        projection_revision=EXACT_CONTEXT_PROJECTION_REVISION,
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        noruct_version=__version__,
        regression=regression,
        alpha_schema_version=alpha.schema_version,
        alpha_report_sha256=alpha_report_sha256,
        alpha_passed_checks=passed_checks,
        alpha_total_checks=len(alpha.checks),
        blocking_checks=alpha.blocking_checks,
        external_model_calls=0,
        quota_consumed=False,
    )
    return ExactContextNaturalEvidence(
        **{
            **to_primitive(base),
            "content_hash": content_digest(base.content_payload()),
            "regression": regression,
            "blocking_checks": alpha.blocking_checks,
        }
    )


def _create_manifest(
    *,
    binding: ExactContextEvidenceBinding,
    preparation: ExactContextBoundPreparation,
    source_revision: str,
    distribution_sha256: str,
    model: str,
    company_revision: int,
    roster_revision: int,
    playbook_revision: int,
    parent_company_state_sha256: str,
    natural_evidence: ExactContextNaturalEvidence,
    lifetime_hours: int,
    max_model_calls_per_run: int,
    max_model_calls_pair: int,
    max_input_tokens_per_run: int,
    max_output_tokens_per_run: int,
    max_cost_usd_per_run: float,
    max_wall_time_ms_per_run: int,
    employee_runtime: str = "native",
) -> ExactContextLivePairManifest:
    if (
        not 1 <= max_model_calls_per_run <= 5
        or max_model_calls_pair != max_model_calls_per_run * 2
        or max_model_calls_pair > 10
    ):
        raise ValueError("Exact-context live pair model-call budget is invalid")
    if (
        not 1_000 <= max_input_tokens_per_run <= 300_000
        or not 256 <= max_output_tokens_per_run <= 20_000
        or not 0 < max_cost_usd_per_run <= 4.0
        or not 1_000 <= max_wall_time_ms_per_run <= 600_000
        or not 1 <= lifetime_hours <= 336
    ):
        raise ValueError("Exact-context live pair token, cost, or time budget is invalid")
    if employee_runtime not in {"native", "noruct"}:
        raise ValueError("Exact-context live pair employee runtime is invalid")
    if tuple(item.strategy for item in preparation.expected_runs) != EXACT_CONTEXT_LIVE_STRATEGIES:
        raise ValueError("Exact-context preparation slot order is invalid")
    created = utc_now().astimezone(timezone.utc)
    base = ExactContextLivePairManifest(
        schema_version=EXACT_CONTEXT_LIVE_PAIR_MANIFEST_SCHEMA,
        pair_id="pending",
        content_hash="pending",
        created_at=created.isoformat(),
        expires_at=(created + timedelta(hours=lifetime_hours)).isoformat(),
        noruct_version=__version__,
        binding_id=binding.binding_id,
        binding_content_hash=binding.content_hash,
        preparation_id=preparation.preparation_id,
        preparation_content_hash=preparation.content_hash,
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        provider_kind="openai-codex-user-managed",
        model_id=model,
        authority_profile=INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
        goal_digest=preparation.goal_digest,
        production_context_fingerprint=preparation.production_context_fingerprint,
        parent_extension_id=preparation.parent_extension_id,
        parent_pattern_id=preparation.parent_pattern_id,
        parent_semantic_anchor=preparation.parent_semantic_anchor,
        parent_company_state_sha256=parent_company_state_sha256,
        bound_pattern_id=preparation.bound_pattern_id,
        natural_evidence_content_hash=natural_evidence.content_hash,
        completion_contract_revision=EXACT_CONTEXT_COMPLETION_CONTRACT_REVISION,
        completion_validator_revision=EXACT_CONTEXT_COMPLETION_VALIDATOR_REVISION,
        max_model_calls_per_run=max_model_calls_per_run,
        max_model_calls_pair=max_model_calls_pair,
        max_input_tokens_per_run=max_input_tokens_per_run,
        max_output_tokens_per_run=max_output_tokens_per_run,
        max_cost_usd_per_run=max_cost_usd_per_run,
        max_wall_time_ms_per_run=max_wall_time_ms_per_run,
        expected_runs=preparation.expected_runs,
        automatic_approval=False,
        eligible_for_apply=False,
        employee_runtime=employee_runtime,
    )
    digest = content_digest(base.content_payload())
    return ExactContextLivePairManifest(
        **{
            **to_primitive(base),
            "pair_id": f"exact-context-live-pair-{digest[:24]}",
            "content_hash": digest,
            "expected_runs": preparation.expected_runs,
        }
    )


def _create_preflight(
    *,
    manifest: ExactContextLivePairManifest,
    checks: tuple[InformationBoundaryCheck, ...],
) -> ExactContextLivePairPreflight:
    base = ExactContextLivePairPreflight(
        schema_version=EXACT_CONTEXT_LIVE_PAIR_PREFLIGHT_SCHEMA,
        preflight_id="pending",
        content_hash="pending",
        recorded_at=utc_now().isoformat(),
        noruct_version=__version__,
        pair_id=manifest.pair_id,
        source_revision=manifest.source_revision,
        distribution_sha256=manifest.distribution_sha256,
        binding_content_hash=manifest.binding_content_hash,
        preparation_content_hash=manifest.preparation_content_hash,
        natural_evidence_content_hash=manifest.natural_evidence_content_hash,
        model_id=manifest.model_id,
        ready=all(check.passed for check in checks),
        checks=checks,
        external_model_calls=0,
        quota_consumed=False,
    )
    digest = content_digest(base.content_payload())
    return ExactContextLivePairPreflight(
        **{
            **to_primitive(base),
            "preflight_id": f"exact-context-live-preflight-{digest[:24]}",
            "content_hash": digest,
            "checks": checks,
        }
    )


def _load_bounded_json(path: Path, *, maximum: int = _RECORD_MAX_BYTES) -> dict[str, object]:
    source = path.expanduser().resolve()
    if (
        not source.is_file()
        or source.is_symlink()
        or source.stat().st_size > maximum
    ):
        raise ValueError("Exact-context live artifact must be a bounded regular file")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Exact-context live artifact cannot be read") from exc
    if not isinstance(value, dict):
        raise ValueError("Exact-context live artifact must be a JSON object")
    return value


def _load_manifest(path: Path) -> ExactContextLivePairManifest:
    value = _load_bounded_json(path)
    if value.get("schema_version") != EXACT_CONTEXT_LIVE_PAIR_MANIFEST_SCHEMA:
        raise ValueError("Exact-context live manifest schema is incompatible")
    expected = tuple(
        ExactContextBoundExpectedRun(**item)
        for item in value.get("expected_runs", [])
        if isinstance(item, dict)
    )
    manifest = ExactContextLivePairManifest(
        **{
            **{key: item for key, item in value.items() if key != "expected_runs"},
            "expected_runs": expected,
        }
    )
    if (
        manifest.content_hash != content_digest(manifest.content_payload())
        or manifest.pair_id
        != f"exact-context-live-pair-{manifest.content_hash[:24]}"
        or len(manifest.expected_runs) != 2
        or tuple(item.strategy for item in manifest.expected_runs)
        != EXACT_CONTEXT_LIVE_STRATEGIES
        or manifest.automatic_approval
        or manifest.eligible_for_apply
    ):
        raise ValueError("Exact-context live manifest contract is invalid")
    return manifest


def _load_preflight(path: Path) -> ExactContextLivePairPreflight:
    value = _load_bounded_json(path)
    if value.get("schema_version") != EXACT_CONTEXT_LIVE_PAIR_PREFLIGHT_SCHEMA:
        raise ValueError("Exact-context live preflight schema is incompatible")
    checks = tuple(
        InformationBoundaryCheck(**item)
        for item in value.get("checks", [])
        if isinstance(item, dict)
    )
    preflight = ExactContextLivePairPreflight(
        **{
            **{key: item for key, item in value.items() if key != "checks"},
            "checks": checks,
        }
    )
    if (
        preflight.content_hash != content_digest(preflight.content_payload())
        or preflight.preflight_id
        != f"exact-context-live-preflight-{preflight.content_hash[:24]}"
        or preflight.ready != all(check.passed for check in checks)
        or preflight.external_model_calls != 0
        or preflight.quota_consumed
    ):
        raise ValueError("Exact-context live preflight contract is invalid")
    return preflight


def _load_natural_evidence(path: Path) -> ExactContextNaturalEvidence:
    value = _load_bounded_json(path)
    if value.get("schema_version") != EXACT_CONTEXT_NATURAL_EVIDENCE_SCHEMA:
        raise ValueError("Exact-context natural evidence schema is incompatible")
    regression = ExactContextRegressionProbe(**value["regression"])
    evidence = ExactContextNaturalEvidence(
        **{
            **{
                key: item
                for key, item in value.items()
                if key not in {"regression", "blocking_checks"}
            },
            "regression": regression,
            "blocking_checks": tuple(value["blocking_checks"]),
        }
    )
    if (
        evidence.content_hash != content_digest(evidence.content_payload())
        or evidence.external_model_calls != 0
        or evidence.quota_consumed
    ):
        raise ValueError("Exact-context natural evidence contract is invalid")
    return evidence


def _pair_artifacts(
    store: ExactContextLivePairStore,
) -> tuple[
    dict[str, object],
    ExactContextLivePairManifest,
    ExactContextLivePairPreflight,
    ExactContextNaturalEvidence,
    dict[str, object],
]:
    metadata = store.metadata()
    if metadata.get("schema_version") != EXACT_CONTEXT_LIVE_PAIR_LEDGER_SCHEMA:
        raise ValueError("Exact-context live ledger schema is invalid")
    paths = {
        "manifest": store.directory / "manifest-v1.json",
        "preflight": store.directory / "preflight-v1.json",
        "natural": store.directory / "natural-evidence-v1.json",
        "alpha": store.directory / "alpha-readiness-v1.json",
    }
    for key, path in paths.items():
        if _sha256_file(path) != metadata.get(f"{key}_file_sha256"):
            raise ValueError("Exact-context live sealed artifact changed")
    manifest = _load_manifest(paths["manifest"])
    preflight = _load_preflight(paths["preflight"])
    natural = _load_natural_evidence(paths["natural"])
    alpha = _load_bounded_json(paths["alpha"])
    if (
        manifest.pair_id != metadata.get("pair_id")
        or preflight.pair_id != manifest.pair_id
        or preflight.source_revision != manifest.source_revision
        or preflight.distribution_sha256 != manifest.distribution_sha256
        or preflight.binding_content_hash != manifest.binding_content_hash
        or preflight.preparation_content_hash != manifest.preparation_content_hash
        or natural.content_hash != manifest.natural_evidence_content_hash
        or natural.alpha_report_sha256 != _sha256_file(paths["alpha"])
        or alpha.get("schema_version") != "noruct.alpha-readiness.v1"
        or alpha.get("external_model_calls") != 0
        or alpha.get("quota_consumed") is not False
    ):
        raise ValueError("Exact-context live artifact identity is inconsistent")
    selected_runtime = str(metadata.get("employee_runtime", "native"))
    if (
        manifest.employee_runtime not in {"native", "noruct"}
        or selected_runtime != manifest.employee_runtime
    ):
        raise ValueError("Exact-context employee runtime identity is inconsistent")
    if manifest.employee_runtime == "noruct":
        worker_python = Path(str(metadata.get("runtime_python") or ""))
        if not worker_python.is_absolute() or not worker_python.is_file():
            raise ValueError("Exact-context Noruct runtime Python is unavailable")
    return metadata, manifest, preflight, natural, alpha


