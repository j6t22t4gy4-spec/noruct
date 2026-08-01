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

from .exact_context_live_pair_primitives import (
    ExactContextLivePairStore,
    _canonical_json,
    _create_manifest,
    _create_natural_evidence,
    _create_preflight,
    _job_limits,
    _load_bounded_json,
    _load_manifest,
    _load_natural_evidence,
    _load_preflight,
    _manifest_fresh,
    _pair_artifacts,
    _run_limits,
    _summary_fields,
    run_python311_regression_probe,
)

def exact_context_live_pair_status(
    directory: str | Path,
) -> ExactContextLivePairStatus:
    with ExactContextLivePairStore(directory) as store:
        _, manifest, preflight, _, _ = _pair_artifacts(store)
        events = store.events()
        root = store.directory
    expected = tuple((item.slot, item.strategy) for item in manifest.expected_runs)
    started: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    recorded: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    failed: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    interrupted: dict[tuple[str, str], FirmValueCampaignEvent] = {}
    for event in events:
        if event.kind == CampaignEventKind.PREPARED:
            continue
        if event.fixture is None or event.strategy is None:
            raise ValueError("Exact-context live ledger event has no slot")
        key = (event.fixture, event.strategy)
        if key not in expected:
            raise ValueError("Exact-context live ledger contains an unknown slot")
        if event.kind == CampaignEventKind.RUN_STARTED:
            if key in started:
                raise ValueError("Exact-context live ledger reuses a run slot")
            started[key] = event
        elif event.kind == CampaignEventKind.RUN_RECORDED:
            if key not in started or key in recorded or key in failed or key in interrupted:
                raise ValueError("Exact-context live record has no unique start")
            recorded[key] = event
        elif event.kind == CampaignEventKind.RUN_FAILED:
            if key not in started or key in recorded or key in failed or key in interrupted:
                raise ValueError("Exact-context live failure has no unique start")
            failed[key] = event
        elif event.kind == CampaignEventKind.RUN_INTERRUPTED:
            if key not in started or key in recorded or key in failed or key in interrupted:
                raise ValueError("Exact-context live interruption has no unique start")
            interrupted[key] = event
        else:
            raise ValueError("Exact-context live ledger event kind is invalid")
    open_slots = {
        key: event
        for key, event in started.items()
        if key not in recorded and key not in failed and key not in interrupted
    }
    abandoned = sum(
        not _process_is_alive(event.payload.get("pid"))
        for event in open_slots.values()
    )
    running = len(open_slots) - abandoned
    unqualified = tuple(
        key
        for key, event in recorded.items()
        if event.payload.get("qualified_for_next") is not True
    )
    manifest_fresh = _manifest_fresh(manifest)
    if not preflight.ready:
        state = ExactContextLivePairState.BLOCKED
        stop_reason = "PREFLIGHT_BLOCKED"
    elif failed or unqualified:
        state = ExactContextLivePairState.PARTIAL_FAILED
        stop_reason = "CONTROL_OR_RUN_FAILED"
    elif interrupted or abandoned:
        state = ExactContextLivePairState.INTERRUPTED
        stop_reason = "RUN_INTERRUPTED"
    elif running:
        state = ExactContextLivePairState.RUNNING
        stop_reason = None
    elif len(recorded) == len(expected):
        state = ExactContextLivePairState.COMPLETE
        stop_reason = None
    elif not manifest_fresh:
        state = ExactContextLivePairState.BLOCKED
        stop_reason = "MANIFEST_EXPIRED"
    else:
        state = ExactContextLivePairState.READY
        stop_reason = None
    next_expected = None
    if state == ExactContextLivePairState.READY:
        next_expected = next(
            (item for item in manifest.expected_runs if (item.slot, item.strategy) not in recorded),
            None,
        )
    record_paths = tuple(
        str(root / str(recorded[key].payload["record_path"]))
        for key in expected
        if key in recorded
    )
    return ExactContextLivePairStatus(
        schema_version="noruct.exact-context-source-frozen-live-status.v1",
        pair_id=manifest.pair_id,
        state=state,
        manifest_content_hash=manifest.content_hash,
        manifest_fresh=manifest_fresh,
        viable=preflight.ready and manifest_fresh and not failed and not unqualified,
        stop_reason=stop_reason,
        completed_runs=len(recorded),
        expected_runs=len(expected),
        failed_runs=len(failed) + len(unqualified),
        interrupted_runs=len(interrupted) + abandoned,
        next_slot=next_expected.slot if next_expected else None,
        next_strategy=next_expected.strategy if next_expected else None,
        max_model_calls_for_next_run=(
            manifest.max_model_calls_per_run if next_expected else 0
        ),
        max_wall_time_ms_for_next_run=(
            manifest.max_wall_time_ms_per_run if next_expected else 0
        ),
        explicit_quota_confirmation_required=next_expected is not None,
        external_model_calls_recorded=sum(
            int(event.payload.get("external_model_calls", 0))
            for event in recorded.values()
        ),
        event_count=len(events),
        ledger_verified=True,
        record_paths=record_paths,
    )


async def prepare_exact_context_live_pair(
    parent_directory: str | Path,
    directory: str | Path,
    *,
    binding_path: str | Path,
    preparation_path: str | Path,
    wheel: str | Path,
    source_root: str | Path,
    model: str,
    command: str,
    python_command: str = sys.executable,
    employee_runtime: str = "native",
    runtime_python: str = sys.executable,
    max_model_calls_per_run: int = 5,
    max_model_calls_pair: int = 10,
    max_input_tokens_per_run: int = 200_000,
    max_output_tokens_per_run: int = 8_000,
    max_cost_usd_per_run: float = 2.0,
    max_wall_time_ms_per_run: int = 180_000,
    lifetime_hours: int = 168,
    request_timeout_seconds: float = 120.0,
    login_status_factory: Callable[[str], CodexLoginStatus] | None = None,
    capability_probe: Callable[[str], tuple[str | None, bool, str]] | None = None,
    regression_probe: Callable[[str | Path], ExactContextRegressionProbe] | None = None,
    alpha_factory: Callable[[str | Path], Awaitable[AlphaReadinessEvaluation]] | None = None,
) -> ExactContextLivePairPreparation:
    target = Path(directory).expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("Exact-context live pair directory must be empty")
    binding = load_exact_context_evidence_binding(binding_path)
    preparation = load_exact_context_bound_preparation(preparation_path)
    parent = _parent_seed(parent_directory)
    source = Path(source_root).expanduser().resolve()
    wheel_path = Path(wheel).expanduser().resolve()
    if not model.strip() or model.strip() != parent.model_id:
        raise ValueError("Exact-context live model must match the immutable parent")
    if employee_runtime not in {"native", "noruct"}:
        raise ValueError("Exact-context live pair employee runtime is invalid")
    # A virtualenv's ``bin/python`` is commonly a symlink to the base
    # interpreter.  Resolving it silently escapes the selected environment
    # (and therefore its explicit employee-runtime dependency profile).
    # Preserve the operator-selected absolute entrypoint exactly.
    worker_python = Path(runtime_python).expanduser()
    if not worker_python.is_absolute():
        worker_python = (Path.cwd() / worker_python).absolute()
    if employee_runtime == "noruct" and not worker_python.is_file():
        raise ValueError("Exact-context Noruct runtime Python must be an absolute file")
    source_revision = source_snapshot_revision(source)
    distribution_sha256 = wheel_distribution_sha256(wheel_path)
    parent_company_path = parent.directory / "isolated-company-extension.db"
    parent_company_sha256 = _sha256_file(parent_company_path)
    bound_prior = workflow_patch_candidate_prior(
        context_fingerprint=binding.production_context_fingerprint
    )
    lineage_matches = (
        preparation.binding_id == binding.binding_id
        and preparation.binding_content_hash == binding.content_hash
        and preparation.source_revision == source_revision
        and preparation.goal_digest == binding.goal_digest
        and preparation.production_context_fingerprint
        == binding.production_context_fingerprint
        and preparation.parent_extension_id == parent.extension_id
        and preparation.parent_pattern_id == parent.pattern_id
        and preparation.parent_semantic_anchor == parent.semantic_anchor
        and preparation.bound_pattern_id == bound_prior.pattern_id
        and not preparation.automatic_approval
        and not preparation.eligible_for_apply
    )
    regression = (
        regression_probe(source)
        if regression_probe is not None
        else run_python311_regression_probe(
            source,
            python_command=python_command,
        )
    )
    alpha = await (alpha_factory or run_alpha_readiness_evaluation)(source)
    alpha_payload = _canonical_json(alpha)
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    alpha_path = _write_private(target / "alpha-readiness-v1.json", alpha_payload)
    natural = _create_natural_evidence(
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        regression=regression,
        alpha=alpha,
        alpha_report_sha256=_sha256_file(alpha_path),
    )
    natural_path = _write_private(
        target / "natural-evidence-v1.json",
        _canonical_json(natural),
    )
    login = (login_status_factory or CodexExecProvider.login_status)(command)
    executable, structured, capability_evidence = (
        capability_probe or probe_codex_structured_output
    )(command)
    checks = (
        InformationBoundaryCheck(
            "exact-binding-and-preparation",
            lineage_matches,
            f"binding={binding.binding_id},preparation={preparation.preparation_id}",
        ),
        InformationBoundaryCheck(
            "source-and-wheel-frozen",
            source_revision == preparation.source_revision
            and len(distribution_sha256) == 64,
            f"source={source_revision},wheel={distribution_sha256}",
        ),
        InformationBoundaryCheck(
            "immutable-company-parent",
            len(parent.semantic_anchor) == 64
            and len(parent_company_sha256) == 64
            and parent.playbook_revision == 2,
            f"extension={parent.extension_id},playbook={parent.playbook_revision}",
        ),
        InformationBoundaryCheck(
            "python311-full-regression",
            regression.passed,
            f"{regression.python_version},tests={regression.test_count},skipped={regression.skipped_count}",
        ),
        InformationBoundaryCheck(
            "provider-free-natural-evidence",
            alpha.external_model_calls == 0
            and not alpha.quota_consumed
            and natural.external_model_calls == 0
            and not natural.quota_consumed
            and tuple(alpha.blocking_checks) == _BLOCKERS,
            f"checks={natural.alpha_passed_checks}/{natural.alpha_total_checks},calls=0",
        ),
        InformationBoundaryCheck(
            "codex-executable-installed",
            bool(login.installed and login.executable and executable),
            executable or login.executable or command,
        ),
        InformationBoundaryCheck(
            "codex-authenticated",
            bool(login.authenticated),
            "official login status passed" if login.authenticated else "authentication not confirmed",
        ),
        InformationBoundaryCheck(
            "structured-output-cli-contract",
            structured,
            capability_evidence,
        ),
        InformationBoundaryCheck(
            "fail-fast-two-slot-budget",
            1 <= max_model_calls_per_run <= 5
            and max_model_calls_pair == max_model_calls_per_run * 2
            and max_model_calls_pair <= 10
            and request_timeout_seconds > 0,
            f"per-run={max_model_calls_per_run},pair={max_model_calls_pair}",
        ),
    )
    manifest = _create_manifest(
        binding=binding,
        preparation=preparation,
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        model=model.strip(),
        company_revision=parent.company_revision,
        roster_revision=parent.roster_revision,
        playbook_revision=parent.playbook_revision,
        parent_company_state_sha256=parent_company_sha256,
        natural_evidence=natural,
        lifetime_hours=lifetime_hours,
        max_model_calls_per_run=max_model_calls_per_run,
        max_model_calls_pair=max_model_calls_pair,
        max_input_tokens_per_run=max_input_tokens_per_run,
        max_output_tokens_per_run=max_output_tokens_per_run,
        max_cost_usd_per_run=max_cost_usd_per_run,
        max_wall_time_ms_per_run=max_wall_time_ms_per_run,
        employee_runtime=employee_runtime,
    )
    preflight = _create_preflight(manifest=manifest, checks=checks)
    manifest_path = _write_private(target / "manifest-v1.json", _canonical_json(manifest))
    preflight_path = _write_private(target / "preflight-v1.json", _canonical_json(preflight))
    with ExactContextLivePairStore(target, create=True) as store:
        store.initialize(
            {
                "schema_version": EXACT_CONTEXT_LIVE_PAIR_LEDGER_SCHEMA,
                "pair_id": manifest.pair_id,
                "manifest_file_sha256": _sha256_file(manifest_path),
                "preflight_file_sha256": _sha256_file(preflight_path),
                "natural_file_sha256": _sha256_file(natural_path),
                "alpha_file_sha256": _sha256_file(alpha_path),
                "source_root": str(source),
                "wheel_path": str(wheel_path),
                "parent_directory": str(parent.directory),
                "binding_path": str(Path(binding_path).expanduser().resolve()),
                "preparation_path": str(Path(preparation_path).expanduser().resolve()),
                "codex_command": executable or command,
                "request_timeout_seconds": request_timeout_seconds,
                "employee_runtime": employee_runtime,
                "runtime_python": str(worker_python),
            }
        )
        store.append(
            CampaignEventKind.PREPARED,
            payload={
                "ready": preflight.ready,
                "expected_runs": 2,
                "external_model_calls": 0,
                "quota_consumed": False,
                "automatic_approval": False,
                "eligible_for_apply": False,
            },
        )
    return ExactContextLivePairPreparation(
        preflight=preflight,
        status=exact_context_live_pair_status(target),
    )


