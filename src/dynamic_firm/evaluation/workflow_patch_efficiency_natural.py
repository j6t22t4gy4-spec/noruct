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
    WORKFLOW_PATCH_NATURAL_GOAL,
    WORKFLOW_PATCH_NATURAL_PREFLIGHT_SCHEMA,
    WorkflowPatchNaturalPreflight,
)
from .workflow_patch_efficiency_primitives import _parent_seed



async def evaluate_workflow_patch_natural_preflight(
    parent_directory: str | Path,
    workspace: str | Path,
    *,
    source_root: str | Path,
    goal: str = WORKFLOW_PATCH_NATURAL_GOAL,
    output_path: str | Path | None = None,
) -> WorkflowPatchNaturalPreflight:
    """Exercise the exact product workspace-identity path without a model call."""

    parent = _parent_seed(parent_directory)
    root = Path(workspace).expanduser().resolve()
    source_revision = source_snapshot_revision(
        Path(source_root).expanduser().resolve()
    )
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Natural workload workspace must be a real directory")
    if not goal.strip():
        raise ValueError("Natural workload goal must be non-empty")

    route = route_interactive_input(goal).route
    manifest: tuple[str, ...] = ()
    manifest_status = "AVAILABLE"
    manifest_error: str | None = None
    tools = WorkspaceReadTools({"workspace": root})
    definition = next(
        item
        for item in tools.definitions()
        if item.name == "list_workspace_files"
    )
    arguments = definition.validator(
        {"workspace_id": "workspace", "path": "."}
    )
    try:
        raw = await definition.handler(arguments, CancellationToken())
        decoded = json.loads(raw)
        if not isinstance(decoded, list) or not all(
            isinstance(item, str) for item in decoded
        ):
            raise ToolValidationError(
                "Workspace manifest did not match the read-only tool contract"
            )
        manifest = tuple(decoded)
    except Exception as exc:
        manifest_status = "BLOCKED"
        manifest_error = f"{type(exc).__name__}: {str(exc)[:160]}"

    identity_status = "READY"
    identity_failure_code: str | None = None
    identity_truncated = False
    try:
        projection = await asyncio.to_thread(
            project_workspace_structure,
            root,
            CompilerExecutionProfile.READ_ONLY.value,
        )
    except WorkspaceProjectionError as exc:
        identity_status = "FAILED"
        identity_failure_code = exc.code.value
        context = ""
    except Exception:
        identity_status = "FAILED"
        identity_failure_code = "INTERNAL_ERROR"
        context = ""
    else:
        identity_truncated = projection.truncated
        context = workflow_context_fingerprint_v2(projection)
    prior = workflow_patch_candidate_prior()
    selected_prior_ids = (
        (prior.pattern_id,)
        if context and context == prior.context_fingerprint
        else ()
    )
    route_passed = route == InputRoute.COMPANY_GOAL
    read_boundary_passed = (
        manifest_status == "AVAILABLE"
        or (
            manifest_status == "BLOCKED"
            and "entry limit" in (manifest_error or "").lower()
        )
    )
    context_passed = identity_status == "READY" and bool(context)
    prior_passed = selected_prior_ids == (parent.pattern_id,)
    checks = (
        InformationBoundaryCheck(
            "immutable-keep-parent",
            parent.pattern_id == prior.pattern_id
            and len(parent.semantic_anchor) == 64,
            (
                f"extension={parent.extension_id},"
                f"pattern={parent.pattern_id}"
            ),
        ),
        InformationBoundaryCheck(
            "natural-goal-routes-to-company",
            route_passed,
            route.value,
        ),
        InformationBoundaryCheck(
            "model-file-list-boundary-preserved",
            read_boundary_passed,
            (
                f"entries={len(manifest)},limit={tools.max_entries}"
                if manifest_status == "AVAILABLE"
                else manifest_error or "workspace manifest unavailable"
            ),
        ),
        InformationBoundaryCheck(
            "bounded-workspace-identity-available",
            context_passed,
            (
                f"context={context},truncated={identity_truncated}"
                if context_passed
                else identity_failure_code or "empty context fingerprint"
            ),
        ),
        InformationBoundaryCheck(
            "applied-prior-selected",
            prior_passed,
            (
                ",".join(selected_prior_ids)
                if selected_prior_ids
                else (
                    f"actual={context or 'none'},"
                    f"applied={prior.context_fingerprint}"
                )
            ),
        ),
        InformationBoundaryCheck(
            "provider-free-preflight",
            True,
            "external-model-calls=0,quota-consumed=false",
        ),
    )
    ready = all(check.passed for check in checks)
    if not context_passed:
        outcome = "NATURAL_WORKLOAD_PREFLIGHT_BLOCKED_BY_WORKSPACE_IDENTITY"
        direction = "inspect-bounded-workspace-identity-failure"
    elif not prior_passed:
        outcome = "NATURAL_WORKLOAD_PREFLIGHT_BLOCKED_BY_PRIOR_CONTEXT"
        direction = "collect-production-exact-context-evidence"
    elif ready:
        outcome = "NATURAL_WORKLOAD_LIVE_OBSERVATION_READY"
        direction = "run-one-bounded-natural-live-observation"
    else:
        outcome = "NATURAL_WORKLOAD_PREFLIGHT_BLOCKED"
        direction = "inspect-preflight-evidence"
    base = WorkflowPatchNaturalPreflight(
        schema_version=WORKFLOW_PATCH_NATURAL_PREFLIGHT_SCHEMA,
        preflight_id="pending",
        content_hash="pending",
        recorded_at=utc_now().isoformat(),
        noruct_version=__version__,
        source_revision=source_revision,
        parent_extension_id=parent.extension_id,
        parent_semantic_anchor=parent.semantic_anchor,
        applied_pattern_id=parent.pattern_id,
        applied_context_fingerprint=prior.context_fingerprint,
        goal_digest=content_digest(goal.strip()),
        route=route.value,
        workspace_manifest_status=manifest_status,
        workspace_manifest_error=manifest_error,
        workspace_manifest_count=len(manifest),
        workspace_manifest_limit=tools.max_entries,
        workspace_identity_status=identity_status,
        workspace_identity_failure_code=identity_failure_code,
        workspace_projection_revision=WORKSPACE_STRUCTURE_PROJECTION_REVISION,
        workspace_projection_truncated=identity_truncated,
        workspace_context_fingerprint=context,
        selected_prior_ids=selected_prior_ids,
        ready_for_live_observation=ready,
        outcome=outcome,
        recommended_direction=direction,
        checks=checks,
        external_model_calls=0,
        quota_consumed=False,
    )
    digest = content_digest(base.content_payload())
    report = WorkflowPatchNaturalPreflight(
        **{
            **to_primitive(base),
            "preflight_id": (
                f"workflow-patch-natural-preflight-{digest[:24]}"
            ),
            "content_hash": digest,
            "selected_prior_ids": selected_prior_ids,
            "checks": checks,
        }
    )
    if output_path is not None:
        _write_private(
            Path(output_path).expanduser().resolve(),
            json.dumps(
                to_primitive(report),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
        )
    return report
