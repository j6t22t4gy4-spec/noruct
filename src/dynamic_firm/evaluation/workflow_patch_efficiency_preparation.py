from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from dynamic_firm import __version__
from dynamic_firm.company import (
    CompanyStateStore,
    WorkflowPatchStatus,
    WORKSPACE_STRUCTURE_PROJECTION_REVISION,
    WorkspaceProjectionError,
    project_workspace_structure,
    workflow_context_fingerprint_v2,
)
from dynamic_firm.company.models import content_digest
from dynamic_firm.compiler import CompilerExecutionProfile
from dynamic_firm.product import InputRoute, route_interactive_input
from dynamic_firm.providers.codex_exec import CodexExecProvider, CodexLoginStatus
from dynamic_firm.runtime.models import to_primitive, utc_now
from dynamic_firm.runtime.ports import (
    CancellationToken,
    ModelProviderError,
    OperationCancelled,
)
from dynamic_firm.runtime.tools import ToolValidationError, WorkspaceReadTools

from .causal_workflow import run_causal_workflow_evaluation
from .context_binding import (
    ExactContextBoundPreparation,
    ExactContextEvidenceBinding,
    create_exact_context_bound_preparation,
    create_exact_context_evidence_binding,
    exact_context_binding_to_json,
    load_exact_context_evidence_binding,
)
from .firm_value import wheel_distribution_sha256
from .firm_value_campaign import (
    CampaignEventKind,
    FirmValueCampaignEvent,
    FirmValueCampaignStore,
    _process_is_alive,
    _sha256_file,
    _write_private,
    probe_codex_structured_output,
    source_snapshot_revision,
)
from .information_boundary import InformationBoundaryCheck
from .workflow_patch_extension import (
    WorkflowPatchExtensionState,
    WorkflowPatchExtensionStore,
    _company_store as _extension_company_store,
    _extension_artifacts,
    workflow_patch_extension_status,
)
from .workflow_patch_live import (
    INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
    WORKFLOW_PATCH_COMPLETION_CONTRACT_REVISION,
    WORKFLOW_PATCH_COMPLETION_CONTRACT_V2_REVISION,
    WORKFLOW_PATCH_COMPLETION_CONTRACT_V3_REVISION,
    WORKFLOW_PATCH_COMPLETION_VALIDATOR_REVISION,
    WORKFLOW_PATCH_EFFICIENCY_STRATEGIES,
    WORKFLOW_PATCH_EFFICIENCY_V2_STRATEGIES,
    WORKFLOW_PATCH_EFFICIENCY_V3_STRATEGIES,
    WORKFLOW_PATCH_LIVE_EVIDENCE_CLASS,
    LiveWorkflowPatchConfig,
    LiveWorkflowPatchRecord,
    WorkflowPatchCompletionAttemptProjection,
    live_workflow_patch_record_to_json,
    load_live_workflow_patch_record,
    run_live_workflow_patch_evaluation,
    workflow_patch_candidate_prior,
    workflow_patch_efficiency_benchmark_revision,
    workflow_patch_efficiency_matched_context_hash,
    workflow_patch_fixture_revision,
    workflow_patch_live_identity,
    workflow_patch_memory_revision,
    workflow_patch_pattern_id,
)


from .workflow_patch_efficiency_contracts import (
    _MAX_RECORDS,
    WORKFLOW_PATCH_EFFICIENCY_LEDGER_SCHEMA,
    WorkflowPatchEfficiencyPreparation,
    WorkflowPatchEfficiencyStore,
)
from .workflow_patch_efficiency_primitives import (
    _create_manifest,
    _create_preflight,
    _parent_seed,
    _slots_for_contract_revision,
)
from .workflow_patch_efficiency_status import workflow_patch_efficiency_status

async def prepare_workflow_patch_efficiency_pair(
    parent_directory: str | Path,
    directory: str | Path,
    *,
    wheel: str | Path,
    source_root: str | Path,
    model: str,
    command: str,
    max_model_calls_per_run: int = 8,
    max_model_calls_pair: int = 16,
    max_wall_time_ms_per_run: int = 180_000,
    lifetime_hours: int = 168,
    request_timeout_seconds: float = 120.0,
    completion_contract_revision: str = (
        WORKFLOW_PATCH_COMPLETION_CONTRACT_REVISION
    ),
    login_status_factory: Callable[[str], CodexLoginStatus] | None = None,
    capability_probe: Callable[[str], tuple[str | None, bool, str]] | None = None,
) -> WorkflowPatchEfficiencyPreparation:
    target = Path(directory).expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(
            f"Completion efficiency directory must be empty: {target}"
        )
    parent = _parent_seed(parent_directory)
    if model.strip() != parent.model_id:
        raise ValueError(
            "Completion efficiency model must match the parent extension"
        )
    source = Path(source_root).expanduser().resolve()
    wheel_path = Path(wheel).expanduser().resolve()
    source_revision = source_snapshot_revision(source)
    distribution_sha256 = wheel_distribution_sha256(wheel_path)
    control = await run_causal_workflow_evaluation()
    control_path: Path
    login = (login_status_factory or CodexExecProvider.login_status)(command)
    executable, structured, capability_evidence = (
        capability_probe or probe_codex_structured_output
    )(command)
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    control_path = _write_private(
        target / "provider-free-control-v1.json",
        json.dumps(
            to_primitive(control),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
    )
    slots = _slots_for_contract_revision(completion_contract_revision)
    strategies = tuple(strategy for _, strategy in slots)
    checks = (
        InformationBoundaryCheck(
            "provider-free-causal-control",
            control.passed
            and control.external_model_calls == 0
            and not control.quota_consumed,
            f"records={control.cohort_job_count},provider-calls=0",
        ),
        InformationBoundaryCheck(
            "immutable-keep-parent",
            len(parent.semantic_anchor) == 64
            and parent.playbook_revision == 2
            and parent.pattern_id == workflow_patch_pattern_id(),
            (
                f"extension={parent.extension_id},"
                f"anchor={parent.semantic_anchor}"
            ),
        ),
        InformationBoundaryCheck(
            "exact-source-frozen-pair",
            strategies
            in {
                WORKFLOW_PATCH_EFFICIENCY_STRATEGIES,
                WORKFLOW_PATCH_EFFICIENCY_V2_STRATEGIES,
                WORKFLOW_PATCH_EFFICIENCY_V3_STRATEGIES,
            },
            " -> ".join(strategies),
        ),
        InformationBoundaryCheck(
            "validator-invariant",
            bool(WORKFLOW_PATCH_COMPLETION_VALIDATOR_REVISION),
            WORKFLOW_PATCH_COMPLETION_VALIDATOR_REVISION,
        ),
        InformationBoundaryCheck(
            "single-prompt-treatment",
            completion_contract_revision
            in {
                WORKFLOW_PATCH_COMPLETION_CONTRACT_REVISION,
                WORKFLOW_PATCH_COMPLETION_CONTRACT_V2_REVISION,
                WORKFLOW_PATCH_COMPLETION_CONTRACT_V3_REVISION,
            },
            completion_contract_revision,
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
            (
                "official login status passed"
                if login.authenticated
                else "authentication not confirmed"
            ),
        ),
        InformationBoundaryCheck(
            "structured-output-cli-contract",
            structured,
            capability_evidence,
        ),
        InformationBoundaryCheck(
            "bounded-live-quota",
            request_timeout_seconds > 0
            and 1 <= max_model_calls_per_run <= 8
            and max_model_calls_pair
            == max_model_calls_per_run * _MAX_RECORDS
            and max_model_calls_pair <= 16,
            (
                f"per-run<={max_model_calls_per_run},"
                f"pair<={max_model_calls_pair}"
            ),
        ),
    )
    preflight = _create_preflight(
        source_revision=source_revision,
        distribution_sha256=distribution_sha256,
        model=model.strip(),
        parent_anchor=parent.semantic_anchor,
        provider_free_control_hash=_sha256_file(control_path),
        checks=checks,
    )
    manifest = _create_manifest(
        preflight,
        parent,
        max_model_calls_per_run=max_model_calls_per_run,
        max_model_calls_pair=max_model_calls_pair,
        max_wall_time_ms_per_run=max_wall_time_ms_per_run,
        lifetime_hours=lifetime_hours,
        completion_contract_revision=completion_contract_revision,
    )
    manifest_path = _write_private(
        target / "manifest-v1.json",
        json.dumps(
            to_primitive(manifest),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
    )
    preflight_path = _write_private(
        target / "preflight-v1.json",
        json.dumps(
            to_primitive(preflight),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
    )
    with WorkflowPatchEfficiencyStore(target, create=True) as store:
        store.initialize(
            {
                "schema_version": WORKFLOW_PATCH_EFFICIENCY_LEDGER_SCHEMA,
                "pair_id": manifest.pair_id,
                "manifest_file_sha256": _sha256_file(manifest_path),
                "preflight_file_sha256": _sha256_file(preflight_path),
                "control_file_sha256": _sha256_file(control_path),
                "source_root": str(source),
                "wheel_path": str(wheel_path),
                "parent_directory": str(parent.directory),
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
                "expected_runs": _MAX_RECORDS,
                "parent_semantic_anchor": parent.semantic_anchor,
                "treatment": "task-local-completion-contract-only",
            },
        )
    return WorkflowPatchEfficiencyPreparation(
        preflight,
        workflow_patch_efficiency_status(target),
    )

