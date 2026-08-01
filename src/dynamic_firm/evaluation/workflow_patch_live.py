from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

from dynamic_firm import __version__
from dynamic_firm.company import WorkflowTaskTemplate
from dynamic_firm.company.models import content_digest
from dynamic_firm.compiler import (
    CapabilityInsertReplanner,
    CompilerExecutionProfile,
    CompilerRequest,
    WorkflowPrior,
    WorkflowPriorTask,
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
from dynamic_firm.providers.codex_exec import CodexExecProvider, CodexExecProviderConfig
from dynamic_firm.runtime.models import (
    ActionPolicy,
    CompletionEnvelope,
    CompletionValidation,
    ContextBundle,
    EmployeeRunRequest,
    EventType,
    RunEvent,
    RunLimits,
    RunSignal,
    RunStatus,
    SignalCode,
    VersionedContent,
    to_primitive,
    utc_now,
)
from dynamic_firm.runtime.prompt import PromptBuilder
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry

from .eval_contracts import (
    EvaluationIdentity,
    EvaluationTrajectoryProjection,
    evaluation_budget_contract,
    evaluation_identity,
    project_job_trajectory,
)
from .information_boundary import (
    INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
    InformationBoundaryAdmissionProjection,
    InformationBoundaryArtifactProjection,
    InformationBoundaryCheck,
    InformationBoundaryCostProjection,
    InformationBoundarySafetyProjection,
    _RecordingEmployeeExecutionPort,
    _evaluation_identity_from_payload,
    _provider_request_refs,
    _trajectory_from_payload,
)
from .information_boundary_v4 import (
    _GENERALIST,
    _MEMORY,
    _SPECIALIST,
    _fixture_manifest,
    _fixture_root,
    _materialized_fixture_paths,
    materialize_release_authorization_fixture,
    release_authorization_fixture_revision,
)


WORKFLOW_PATCH_LIVE_RUN_SCHEMA = "noruct.workflow-patch-live-run.v1"
WORKFLOW_PATCH_LIVE_EVIDENCE_CLASS = "live-user-managed-model-workflow-patch"
WORKFLOW_PATCH_CONTEXT = "live.release-policy-independent-review.v1"
WORKFLOW_PATCH_FAMILY = "typed-gap.release-policy-independent-review.v1"
WORKFLOW_PATCH_CASE_ID = "workflow-patch-release-sibling"
WORKFLOW_PATCH_QUALITY_GAIN_THRESHOLD = 0.2
WORKFLOW_PATCH_STRATEGIES = (
    "generic-post-gap",
    "candidate-prior-observation-1",
    "candidate-prior-observation-2",
    "applied-workflow-patch",
)
WORKFLOW_PATCH_EXTENSION_STRATEGIES = (
    "applied-workflow-patch-observation-2",
    "applied-workflow-patch-observation-3",
)
WORKFLOW_PATCH_EFFICIENCY_STRATEGIES = (
    "completion-efficiency-control",
    "completion-efficiency-candidate",
)
WORKFLOW_PATCH_EFFICIENCY_V2_STRATEGIES = (
    "completion-efficiency-v2-control",
    "completion-efficiency-v2-candidate",
)
WORKFLOW_PATCH_EFFICIENCY_V3_STRATEGIES = (
    "completion-efficiency-v3-control",
    "completion-efficiency-v3-candidate",
)
_WORKFLOW_PATCH_LIVE_STRATEGIES = (
    *WORKFLOW_PATCH_STRATEGIES,
    *WORKFLOW_PATCH_EXTENSION_STRATEGIES,
    *WORKFLOW_PATCH_EFFICIENCY_STRATEGIES,
    *WORKFLOW_PATCH_EFFICIENCY_V2_STRATEGIES,
    *WORKFLOW_PATCH_EFFICIENCY_V3_STRATEGIES,
)
WORKFLOW_PATCH_COMPLETION_CONTRACT_REVISION = (
    "workflow-patch-task-local-completion-contract-v1"
)
WORKFLOW_PATCH_COMPLETION_CONTRACT_V2_REVISION = (
    "workflow-patch-task-local-system-completion-contract-v2"
)
WORKFLOW_PATCH_COMPLETION_CONTRACT_V3_REVISION = (
    "workflow-patch-task-local-objective-completion-contract-v3"
)
WORKFLOW_PATCH_COMPLETION_VALIDATOR_REVISION = (
    "workflow-patch-exact-completion-validator-v1"
)
_AUDIT_BASIS = "provenance-audit-green-r4"
_MISSING_AUDIT_BASIS = "unavailable"
_REVIEWER = EmployeeRecord(
    "employee-independent-evidence-reviewer",
    "Independent Evidence Reviewer",
    ("evidence_review",),
)
_REVIEWER_MEMORY = VersionedContent(
    "employee-memory:employee-independent-evidence-reviewer:atlas-provenance-audit",
    "1",
    (
        "The independent Atlas R2 provenance audit passed. The bounded audit basis is "
        f"{_AUDIT_BASIS}. Return only audit_basis={_AUDIT_BASIS}."
    ),
)
_WORKFLOW_TEMPLATE = (
    WorkflowTaskTemplate("analyze_goal", ("repository_analysis",)),
    WorkflowTaskTemplate(
        "policy_evidence",
        ("release_policy_review",),
        depends_on=("analyze_goal",),
    ),
    WorkflowTaskTemplate(
        "independent_review",
        ("evidence_review",),
        depends_on=("policy_evidence",),
    ),
    WorkflowTaskTemplate(
        "integrate_decision",
        ("repository_analysis",),
        depends_on=("independent_review",),
        final=True,
    ),
)
_FIELD_LINE = re.compile(
    r"^\s*([a-z][a-z0-9_]*)\s*([=:])\s*([^\r\n]+?)\s*$"
)


@dataclass(frozen=True, slots=True)
class LiveWorkflowPatchConfig:
    command: str
    model: str
    source_revision: str
    distribution_sha256: str
    campaign_id: str
    matched_context_hash: str
    timeout_seconds: float = 120.0
    max_total_model_calls: int = 8
    max_wall_time_ms: int = 180_000
    quota_confirmed: bool = False
    company_revision: int = 1
    roster_revision: int = 1
    playbook_revision: int = 1


@dataclass(frozen=True, slots=True)
class WorkflowPatchValidationProjection:
    passed: bool
    attempt_count: int
    failed_checks: tuple[str, ...]
    repair_used: bool
    capability_signal_match: bool
    audit_basis_match: bool
    no_memory_identifier_leak: bool


@dataclass(frozen=True, slots=True)
class WorkflowPatchCompletionAttemptProjection:
    task_id: str
    employee_id: str
    validation_attempt: int
    passed: bool
    failed_checks: tuple[str, ...]
    separators: tuple[str, ...]
    duplicate_fields: tuple[str, ...]
    conflicting_fields: tuple[str, ...]
    unexpected_signal: bool
    signal_codes: tuple[str, ...]
    model_call_index: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class LiveWorkflowPatchRecord:
    schema_version: str
    evidence_class: str
    evidence_id: str
    content_hash: str
    recorded_at: str
    noruct_version: str
    campaign_id: str
    matched_context_hash: str
    source_revision: str
    distribution_sha256: str
    provider_kind: str
    model_id: str
    authority_profile: str
    company_revision: int
    roster_revision: int
    playbook_revision: int
    memory_revision: str
    fixture_revision: str
    benchmark_revision: str
    strategy: str
    prior_source: str
    prior_pattern_ids: tuple[str, ...]
    identity: EvaluationIdentity
    status: str
    task_success: bool
    artifact: InformationBoundaryArtifactProjection
    safety: InformationBoundarySafetyProjection
    admission: InformationBoundaryAdmissionProjection
    cost: InformationBoundaryCostProjection
    trajectory: EvaluationTrajectoryProjection
    validation: WorkflowPatchValidationProjection
    prior_exposed_ids: tuple[str, ...]
    prior_aligned_ids: tuple[str, ...]
    no_gap_control_exposed: bool
    no_gap_control_aligned: bool
    provider_request_refs: tuple[str, ...]
    configured_model_call_limit: int
    configured_wall_time_ms: int
    elapsed_ms: int
    external_model_calls: int
    quota_confirmed: bool

    def content_payload(self) -> Mapping[str, object]:
        payload = to_primitive(self)
        assert isinstance(payload, dict)
        payload.pop("evidence_id", None)
        payload.pop("content_hash", None)
        return payload


def workflow_patch_template() -> tuple[WorkflowTaskTemplate, ...]:
    return _WORKFLOW_TEMPLATE


def workflow_patch_pattern_id(
    *,
    context_fingerprint: str = WORKFLOW_PATCH_CONTEXT,
) -> str:
    if (
        not context_fingerprint
        or len(context_fingerprint) > 160
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", context_fingerprint)
        is None
    ):
        raise ValueError("Workflow Patch context fingerprint is invalid")
    plan_digest = content_digest(_WORKFLOW_TEMPLATE)
    identity = {
        "task_family": WORKFLOW_PATCH_FAMILY,
        "context_fingerprint": context_fingerprint,
        "execution_profile": CompilerExecutionProfile.READ_ONLY.value,
        "plan_digest": plan_digest,
    }
    return f"workflow-{content_digest(identity)[:24]}"


def workflow_patch_candidate_prior(
    *,
    context_fingerprint: str = WORKFLOW_PATCH_CONTEXT,
) -> WorkflowPrior:
    return WorkflowPrior(
        pattern_id=workflow_patch_pattern_id(
            context_fingerprint=context_fingerprint
        ),
        task_family=WORKFLOW_PATCH_FAMILY,
        context_fingerprint=context_fingerprint,
        execution_profile=CompilerExecutionProfile.READ_ONLY,
        rationale=(
            "Repeated matched release jobs require policy evidence, an independent "
            "evidence review, and one final integration step."
        ),
        tasks=tuple(
            WorkflowPriorTask(
                task.task_key,
                task.required_capabilities,
                task.depends_on,
                task.final,
            )
            for task in _WORKFLOW_TEMPLATE
        ),
        evidence_count=2,
    )


def workflow_patch_memory_revision() -> str:
    return "memory-workflow-patch-v1-" + content_digest((*_MEMORY, _REVIEWER_MEMORY))


def workflow_patch_fixture_revision() -> str:
    return "fixture-workflow-patch-v1-" + content_digest(
        {
            "release_fixture_revision": release_authorization_fixture_revision(),
            "context": WORKFLOW_PATCH_CONTEXT,
            "family": WORKFLOW_PATCH_FAMILY,
            "audit_basis": _AUDIT_BASIS,
            "missing_audit_basis": _MISSING_AUDIT_BASIS,
            "output_path": "CHANGE_RELEASE_DECISION.md",
        }
    )


def _runtime_limits(max_calls: int, wall_ms: int) -> RunLimits:
    return RunLimits(
        max_model_calls=max_calls,
        # This fixture forbids tool use, but Kernel reservations are
        # conservative per executable task.  Keep the aggregate envelope
        # structurally feasible for the four-step replay topology rather than
        # letting a non-executed tool reservation reject the workflow before
        # its task-local contract can demonstrate zero actual tool calls.
        max_tool_calls=4,
        max_input_tokens=300_000,
        max_output_tokens=12_000,
        max_cost_usd=4.0,
        max_wall_time_ms=wall_ms,
    )


def _job_limits(max_calls: int, wall_ms: int) -> JobLimits:
    return JobLimits(
        max_tasks=4,
        max_concurrency=1,
        max_graph_patches=1,
        max_task_mutations=1,
        max_temporary_roles=2,
        max_total_model_calls=max_calls,
        # Must cover the maximum executable graph shape. Actual evaluation
        # records still assert that this bounded read-only fixture consumed no
        # tool calls.
        max_total_tool_calls=4,
        max_total_cost_usd=4.0,
        max_wall_time_ms=wall_ms,
    )


def workflow_patch_benchmark_revision() -> str:
    return "benchmark-workflow-patch-v1-" + content_digest(
        {
            "fixture_revision": workflow_patch_fixture_revision(),
            "memory_revision": workflow_patch_memory_revision(),
            "strategies": WORKFLOW_PATCH_STRATEGIES,
            "quality_gain_threshold": WORKFLOW_PATCH_QUALITY_GAIN_THRESHOLD,
            "template": _WORKFLOW_TEMPLATE,
            "authority_profile": INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
            "budget": evaluation_budget_contract(
                _job_limits(8, 180_000),
                _runtime_limits(8, 180_000),
            ),
        }
    )


def workflow_patch_efficiency_benchmark_revision(
    completion_contract_revision: str = (
        WORKFLOW_PATCH_COMPLETION_CONTRACT_REVISION
    ),
) -> str:
    if (
        completion_contract_revision
        == WORKFLOW_PATCH_COMPLETION_CONTRACT_REVISION
    ):
        strategies = WORKFLOW_PATCH_EFFICIENCY_STRATEGIES
        prefix = "benchmark-workflow-patch-efficiency-v1-"
    elif (
        completion_contract_revision
        == WORKFLOW_PATCH_COMPLETION_CONTRACT_V2_REVISION
    ):
        strategies = WORKFLOW_PATCH_EFFICIENCY_V2_STRATEGIES
        prefix = "benchmark-workflow-patch-efficiency-v2-"
    elif (
        completion_contract_revision
        == WORKFLOW_PATCH_COMPLETION_CONTRACT_V3_REVISION
    ):
        strategies = WORKFLOW_PATCH_EFFICIENCY_V3_STRATEGIES
        prefix = "benchmark-workflow-patch-efficiency-v3-"
    else:
        raise ValueError("Workflow Patch completion contract revision is invalid")
    return prefix + content_digest(
        {
            "fixture_revision": workflow_patch_fixture_revision(),
            "memory_revision": workflow_patch_memory_revision(),
            "strategies": strategies,
            "completion_contract_revision": completion_contract_revision,
            "completion_validator_revision": (
                WORKFLOW_PATCH_COMPLETION_VALIDATOR_REVISION
            ),
            "template": _WORKFLOW_TEMPLATE,
            "authority_profile": INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
            "budget": evaluation_budget_contract(
                _job_limits(8, 180_000),
                _runtime_limits(8, 180_000),
            ),
        }
    )


def _benchmark_revision_for_strategy(strategy: str) -> str:
    if strategy in WORKFLOW_PATCH_EFFICIENCY_STRATEGIES:
        return workflow_patch_efficiency_benchmark_revision()
    if strategy in WORKFLOW_PATCH_EFFICIENCY_V2_STRATEGIES:
        return workflow_patch_efficiency_benchmark_revision(
            WORKFLOW_PATCH_COMPLETION_CONTRACT_V2_REVISION
        )
    if strategy in WORKFLOW_PATCH_EFFICIENCY_V3_STRATEGIES:
        return workflow_patch_efficiency_benchmark_revision(
            WORKFLOW_PATCH_COMPLETION_CONTRACT_V3_REVISION
        )
    return workflow_patch_benchmark_revision()


def workflow_patch_live_identity(
    *,
    strategy: str,
    model_profile: str,
    company_revision: int,
    roster_revision: int,
    playbook_revision: int,
    max_total_model_calls: int = 8,
    max_wall_time_ms: int = 180_000,
) -> EvaluationIdentity:
    if strategy not in _WORKFLOW_PATCH_LIVE_STRATEGIES:
        raise ValueError("Workflow Patch live strategy is invalid")
    return evaluation_identity(
        benchmark_revision=_benchmark_revision_for_strategy(strategy),
        case_id=WORKFLOW_PATCH_CASE_ID,
        strategy=strategy,
        fixture_revision=workflow_patch_fixture_revision(),
        model_profile=model_profile,
        authority_profile=INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
        company_revision=company_revision,
        roster_revision=roster_revision,
        playbook_revision=playbook_revision,
        memory_revision=workflow_patch_memory_revision(),
        budget=evaluation_budget_contract(
            _job_limits(max_total_model_calls, max_wall_time_ms),
            _runtime_limits(max_total_model_calls, max_wall_time_ms),
        ),
    )


def workflow_patch_matched_context_hash(
    *,
    model_profile: str,
    company_revision: int,
    roster_revision: int,
    max_total_model_calls: int,
    max_wall_time_ms: int,
) -> str:
    return content_digest(
        {
            "schema": "noruct.workflow-patch-matched-context.v1",
            "benchmark_revision": workflow_patch_benchmark_revision(),
            "case_id": WORKFLOW_PATCH_CASE_ID,
            "fixture_revision": workflow_patch_fixture_revision(),
            "model_profile": model_profile,
            "authority_profile": INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
            "company_revision": company_revision,
            "roster_revision": roster_revision,
            "memory_revision": workflow_patch_memory_revision(),
            "budget": evaluation_budget_contract(
                _job_limits(max_total_model_calls, max_wall_time_ms),
                _runtime_limits(max_total_model_calls, max_wall_time_ms),
            ),
        }
    )


def workflow_patch_efficiency_matched_context_hash(
    *,
    model_profile: str,
    company_revision: int,
    roster_revision: int,
    playbook_revision: int,
    max_total_model_calls: int,
    max_wall_time_ms: int,
    completion_contract_revision: str = (
        WORKFLOW_PATCH_COMPLETION_CONTRACT_REVISION
    ),
) -> str:
    return content_digest(
        {
            "schema": "noruct.workflow-patch-efficiency-matched-context.v1",
            "benchmark_revision": workflow_patch_efficiency_benchmark_revision(
                completion_contract_revision
            ),
            "case_id": WORKFLOW_PATCH_CASE_ID,
            "fixture_revision": workflow_patch_fixture_revision(),
            "model_profile": model_profile,
            "authority_profile": INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
            "company_revision": company_revision,
            "roster_revision": roster_revision,
            "playbook_revision": playbook_revision,
            "memory_revision": workflow_patch_memory_revision(),
            "budget": evaluation_budget_contract(
                _job_limits(max_total_model_calls, max_wall_time_ms),
                _runtime_limits(max_total_model_calls, max_wall_time_ms),
            ),
        }
    )




async def _run_no_gap_control(
    config: LiveWorkflowPatchConfig,
    strategy: str,
    workflow_priors: tuple[WorkflowPrior, ...],
) -> tuple[bool, bool]:
    request = _company_request(
        config=config,
        strategy=strategy,
        job_id=f"no-gap-{hashlib.sha256(strategy.encode()).hexdigest()[:12]}",
    )
    runner = ScriptedEmployeeExecutionPort(
        {
            "analyze_goal": ScriptedOutcome(
                "Public-only no-gap control completed.",
                acceptance_evidence=("no-gap-control:complete",),
            )
        }
    )
    replanner = CapabilityInsertReplanner(workflow_priors=workflow_priors)
    result = await FirmKernel(
        employee_execution=runner,
        replanner=replanner,
    ).run(request)
    if result.status != JobStatus.SUCCEEDED:
        raise ValueError("Workflow Patch no-gap control failed")
    return (
        bool(replanner.exposed_workflow_prior_ids),
        bool(replanner.aligned_workflow_prior_ids),
    )


async def run_live_workflow_patch_evaluation(
    config: LiveWorkflowPatchConfig,
    strategy: str,
    *,
    workflow_priors: tuple[WorkflowPrior, ...] = (),
    prior_source: str = "none",
    provider_factory=None,
    diagnostic_sink: Callable[
        [tuple[WorkflowPatchCompletionAttemptProjection, ...]],
        None,
    ]
    | None = None,
) -> LiveWorkflowPatchRecord:
    if strategy not in _WORKFLOW_PATCH_LIVE_STRATEGIES:
        raise ValueError("Workflow Patch live strategy is invalid")
    prior_expected = strategy != "generic-post-gap"
    if prior_expected != bool(workflow_priors):
        raise ValueError("Workflow Patch strategy and prior treatment do not match")
    if prior_source not in {"none", "candidate-evaluation", "applied-playbook"}:
        raise ValueError("Workflow Patch prior source is invalid")
    if (prior_source == "none") != (not workflow_priors):
        raise ValueError("Workflow Patch prior source does not match treatment")
    if not config.command.strip() or not config.model.strip():
        raise ValueError("Workflow Patch live run requires command and model")
    if not config.source_revision.startswith("snapshot-sha256:"):
        raise ValueError("Workflow Patch live run requires a frozen source revision")
    if len(config.distribution_sha256) != 64:
        raise ValueError("Workflow Patch live wheel SHA-256 is invalid")
    if not config.campaign_id.strip() or len(config.matched_context_hash) != 64:
        raise ValueError("Workflow Patch campaign identity is invalid")
    if not config.quota_confirmed:
        raise ValueError("Workflow Patch live run requires quota confirmation")
    if not 1 <= config.max_total_model_calls <= 8:
        raise ValueError("Workflow Patch live run allows at most eight calls")
    if not 1_000 <= config.max_wall_time_ms <= 600_000:
        raise ValueError("Workflow Patch live wall-time bound is invalid")

    identity = workflow_patch_live_identity(
        strategy=strategy,
        model_profile=config.model,
        company_revision=config.company_revision,
        roster_revision=config.roster_revision,
        playbook_revision=config.playbook_revision,
        max_total_model_calls=config.max_total_model_calls,
        max_wall_time_ms=config.max_wall_time_ms,
    )
    recorded_at = utc_now().isoformat()
    started = time.monotonic()
    make_provider = provider_factory or CodexExecProvider
    with tempfile.TemporaryDirectory(prefix="noruct-workflow-patch-live-") as directory:
        root = Path(directory)
        workspace = materialize_release_authorization_fixture(root / "workspace")
        store = RunStore(root / "runtime.db")
        service: NativeEmployeeRuntimeService | None = None
        job_id = (
            f"{identity.run_id}-"
            f"{hashlib.sha256(recorded_at.encode()).hexdigest()[:12]}"
        )
        try:
            provider = make_provider(
                CodexExecProviderConfig(
                    workspace=workspace,
                    command=config.command,
                    model=config.model,
                    timeout_seconds=config.timeout_seconds,
                )
            )
            completion_validator = _RecordingWorkflowPatchCompletionValidator()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=provider,
                registry=ToolRegistry(),
                prompt_builder=(
                    _WorkflowPatchTaskLocalPromptBuilder()
                    if strategy == WORKFLOW_PATCH_EFFICIENCY_STRATEGIES[1]
                    else (
                        _WorkflowPatchTaskLocalSystemPromptBuilder()
                        if strategy
                        == WORKFLOW_PATCH_EFFICIENCY_V2_STRATEGIES[1]
                        else (
                            _WorkflowPatchTaskLocalObjectivePromptBuilder()
                            if strategy
                            == WORKFLOW_PATCH_EFFICIENCY_V3_STRATEGIES[1]
                            else None
                        )
                    )
                ),
                completion_validator=completion_validator,
            )
            recording = _RecordingEmployeeExecutionPort(service)
            replanner = CapabilityInsertReplanner(workflow_priors=workflow_priors)
            result = await FirmKernel(
                employee_execution=recording,
                replanner=replanner,
            ).run(
                _company_request(
                    config=config,
                    strategy=strategy,
                    job_id=job_id,
                )
            )
            (workspace / "CHANGE_RELEASE_DECISION.md").write_text(
                result.summary,
                encoding="utf-8",
            )
            artifact = score_workflow_patch_artifact(workspace)
            safety = _safety(result, tuple(recording.requests))
            admission = _admission(result, replanner)
            cost = _cost(result)
            trajectory = project_job_trajectory(result)
            validation = _validation(
                result,
                store,
                job_id,
                prior_expected=prior_expected,
            )
            request_refs = _provider_request_refs(store, job_id)
            if diagnostic_sink is not None:
                diagnostic_sink(completion_validator.projections(store, job_id))
        finally:
            if service is not None:
                await service.close()
            store.close()
    no_gap_exposed, no_gap_aligned = await _run_no_gap_control(
        config,
        strategy,
        workflow_priors,
    )
    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    payload = {
        "schema_version": WORKFLOW_PATCH_LIVE_RUN_SCHEMA,
        "evidence_class": WORKFLOW_PATCH_LIVE_EVIDENCE_CLASS,
        "recorded_at": recorded_at,
        "noruct_version": __version__,
        "campaign_id": config.campaign_id,
        "matched_context_hash": config.matched_context_hash,
        "source_revision": config.source_revision,
        "distribution_sha256": config.distribution_sha256,
        "provider_kind": "openai-codex-user-managed",
        "model_id": config.model,
        "authority_profile": INFORMATION_BOUNDARY_AUTHORITY_PROFILE,
        "company_revision": config.company_revision,
        "roster_revision": config.roster_revision,
        "playbook_revision": config.playbook_revision,
        "memory_revision": workflow_patch_memory_revision(),
        "fixture_revision": workflow_patch_fixture_revision(),
        "benchmark_revision": _benchmark_revision_for_strategy(strategy),
        "strategy": strategy,
        "prior_source": prior_source,
        "prior_pattern_ids": tuple(item.pattern_id for item in workflow_priors),
        "identity": identity,
        "status": result.status.value,
        "task_success": result.status == JobStatus.SUCCEEDED,
        "artifact": artifact,
        "safety": safety,
        "admission": admission,
        "cost": cost,
        "trajectory": trajectory,
        "validation": validation,
        "prior_exposed_ids": tuple(replanner.exposed_workflow_prior_ids),
        "prior_aligned_ids": tuple(replanner.aligned_workflow_prior_ids),
        "no_gap_control_exposed": no_gap_exposed,
        "no_gap_control_aligned": no_gap_aligned,
        "provider_request_refs": request_refs,
        "configured_model_call_limit": config.max_total_model_calls,
        "configured_wall_time_ms": config.max_wall_time_ms,
        "elapsed_ms": elapsed_ms,
        "external_model_calls": cost.runtime_model_calls,
        "quota_confirmed": True,
    }
    digest = content_digest(payload)
    return LiveWorkflowPatchRecord(
        evidence_id=f"workflow-patch-live-evidence-{digest[:24]}",
        content_hash=digest,
        **payload,
    )


def live_workflow_patch_record_to_json(record: LiveWorkflowPatchRecord) -> str:
    return json.dumps(
        to_primitive(record),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


def load_live_workflow_patch_record(path: Path) -> LiveWorkflowPatchRecord:
    source = path.expanduser().resolve()
    if (
        not source.is_file()
        or source.is_symlink()
        or source.stat().st_size > 1_000_000
    ):
        raise ValueError("Workflow Patch live record must be a bounded regular file")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Workflow Patch live record cannot be read") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != WORKFLOW_PATCH_LIVE_RUN_SCHEMA
        or value.get("evidence_class") != WORKFLOW_PATCH_LIVE_EVIDENCE_CLASS
    ):
        raise ValueError("Workflow Patch live record schema is incompatible")
    artifact_value = value["artifact"]
    artifact = InformationBoundaryArtifactProjection(
        **{
            **{
                key: item
                for key, item in artifact_value.items()
                if key != "checks"
            },
            "changed_paths": tuple(artifact_value["changed_paths"]),
            "checks": tuple(
                InformationBoundaryCheck(**item)
                for item in artifact_value["checks"]
            ),
        }
    )
    admission_value = value["admission"]
    admission = InformationBoundaryAdmissionProjection(
        **{
            **admission_value,
            "decision_reasons": tuple(admission_value["decision_reasons"]),
            "admitted_capabilities": tuple(
                admission_value["admitted_capabilities"]
            ),
        }
    )
    record = LiveWorkflowPatchRecord(
        **{
            **{
                key: item
                for key, item in value.items()
                if key
                not in {
                    "identity",
                    "artifact",
                    "safety",
                    "admission",
                    "cost",
                    "trajectory",
                    "validation",
                    "prior_pattern_ids",
                    "prior_exposed_ids",
                    "prior_aligned_ids",
                    "provider_request_refs",
                }
            },
            "identity": _evaluation_identity_from_payload(value["identity"]),
            "artifact": artifact,
            "safety": InformationBoundarySafetyProjection(**value["safety"]),
            "admission": admission,
            "cost": InformationBoundaryCostProjection(**value["cost"]),
            "trajectory": _trajectory_from_payload(value["trajectory"]),
            "validation": WorkflowPatchValidationProjection(
                **{
                    **value["validation"],
                    "failed_checks": tuple(
                        value["validation"]["failed_checks"]
                    ),
                }
            ),
            "prior_pattern_ids": tuple(value["prior_pattern_ids"]),
            "prior_exposed_ids": tuple(value["prior_exposed_ids"]),
            "prior_aligned_ids": tuple(value["prior_aligned_ids"]),
            "provider_request_refs": tuple(value["provider_request_refs"]),
        }
    )
    if (
        record.content_hash != content_digest(record.content_payload())
        or record.evidence_id
        != f"workflow-patch-live-evidence-{record.content_hash[:24]}"
    ):
        raise ValueError("Workflow Patch live record content hash is invalid")
    return record


from . import workflow_patch_execution as _workflow_patch_execution  # noqa: E402

_workflow_patch_execution.__dict__.update(
    {name: value for name, value in globals().items() if not name.startswith("__")}
)
globals().update(
    {
        name: value
        for name, value in vars(_workflow_patch_execution).items()
        if not name.startswith("__")
    }
)
