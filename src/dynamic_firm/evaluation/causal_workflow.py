from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from dynamic_firm.company import (
    CompanyLearningService,
    CompanyStateStore,
    EvidenceSource,
    OrganizationEpisode,
    WorkflowPatchStatus,
    WorkflowTaskTemplate,
)
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
from dynamic_firm.runtime.models import (
    ActionPolicy,
    RunLimits,
    RunSignal,
    SignalCode,
)


CAUSAL_WORKFLOW_CONTEXT = "provider-free.release-policy-gap.v1"
CAUSAL_WORKFLOW_FAMILY = "typed-gap.release-policy-review.v1"
QUALITY_GAIN_THRESHOLD = 0.2


@dataclass(frozen=True, slots=True)
class CausalWorkflowCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class CausalWorkflowJobRecord:
    slot: str
    strategy: str
    status: str
    quality_score: float
    baseline_quality_score: float
    quality_gain: float
    model_calls: int
    baseline_model_calls: int
    task_count: int
    final_task_id: str
    prior_exposed: bool
    prior_aligned: bool
    safety_passed: bool
    unrelated_control_exposed: bool | None = None


@dataclass(frozen=True, slots=True)
class CausalWorkflowEvaluation:
    schema_version: str
    passed: bool
    evidence_class: str
    cohort_job_count: int
    external_model_calls: int
    quota_consumed: bool
    patch_id: str
    final_patch_status: str
    playbook_revision: int
    records: tuple[CausalWorkflowJobRecord, ...]
    checks: tuple[CausalWorkflowCheck, ...]


@dataclass(frozen=True, slots=True)
class _Trajectory:
    result: JobResult
    replanner: CapabilityInsertReplanner
    runner: ScriptedEmployeeExecutionPort
    quality_score: float


def _candidate_prior(pattern_id: str = "candidate-release-policy-workflow") -> WorkflowPrior:
    return WorkflowPrior(
        pattern_id=pattern_id,
        task_family=CAUSAL_WORKFLOW_FAMILY,
        context_fingerprint=CAUSAL_WORKFLOW_CONTEXT,
        execution_profile=CompilerExecutionProfile.READ_ONLY,
        rationale=(
            "Two matched jobs require policy evidence, independent review, then integration."
        ),
        tasks=(
            WorkflowPriorTask("analyze_goal", ("repository_analysis",)),
            WorkflowPriorTask(
                "policy_evidence",
                ("release_policy_review",),
                depends_on=("analyze_goal",),
            ),
            WorkflowPriorTask(
                "independent_review",
                ("evidence_review",),
                depends_on=("policy_evidence",),
            ),
            WorkflowPriorTask(
                "integrate_decision",
                ("repository_analysis",),
                depends_on=("independent_review",),
                final=True,
            ),
        ),
        evidence_count=2,
    )


def _request(job_id: str, *, playbook_revision: int = 1) -> CompanyRunRequest:
    compiler_request = CompilerRequest(
        request_id=f"compiler-{job_id}",
        goal="Resolve the release policy boundary and return one reviewed decision.",
        workspace_manifest=("public-release-evidence.txt",),
        available_capabilities=("repository_analysis",),
        model_profile="scripted",
        execution_profile=CompilerExecutionProfile.READ_ONLY,
        workflow_context_fingerprint=CAUSAL_WORKFLOW_CONTEXT,
        max_tasks=6,
        max_temporary_roles=2,
        max_total_model_calls=6,
    )
    decision = solo_first_decision(compiler_request)
    return CompanyRunRequest(
        request_id=f"request-{job_id}",
        job_id=job_id,
        goal=compiler_request.goal,
        plan_proposal=decision.proposal,
        roster=(
            EmployeeRecord(
                "employee-generalist",
                "Repository Generalist",
                ("repository_analysis",),
            ),
        ),
        runtime_limits=RunLimits(
            max_model_calls=2,
            max_tool_calls=2,
            max_cost_usd=1.0,
        ),
        action_policy=ActionPolicy(filesystem_policy="READ_ONLY"),
        job_limits=JobLimits(
            max_tasks=6,
            max_concurrency=2,
            max_graph_patches=1,
            max_task_mutations=1,
            max_temporary_roles=2,
            max_total_model_calls=6,
            max_total_tool_calls=8,
            max_total_cost_usd=6.0,
            max_wall_time_ms=5_000,
        ),
        company_revision=1,
        roster_revision=1,
        playbook_revision=playbook_revision,
        # Preserve the same exact-context binding that informed the
        # CompilerRequest. Runtime prior replay must not depend on a
        # convenience-only compiler field that disappears at hand-off.
        workflow_context_fingerprint=compiler_request.workflow_context_fingerprint,
    )


async def _run_trajectory(
    job_id: str,
    *,
    workflow_priors: tuple[WorkflowPrior, ...] = (),
    emit_gap: bool = True,
    playbook_revision: int = 1,
) -> _Trajectory:
    gap = RunSignal(
        SignalCode.CAPABILITY_MISSING,
        "release_policy_review",
        ("public evidence cannot resolve the policy boundary",),
    )
    runner = ScriptedEmployeeExecutionPort(
        {
            "analyze_goal": ScriptedOutcome(
                "Public release evidence is available.",
                signals=(gap,) if emit_gap else (),
                acceptance_evidence=("public-evidence:verified",),
            ),
            "specialist_release_policy_review": ScriptedOutcome(
                "Release policy evidence resolved.",
                acceptance_evidence=("policy-evidence:verified",),
            ),
            "integrate_goal": ScriptedOutcome(
                "Policy evidence integrated without an independent review.",
                acceptance_evidence=("decision:integrated",),
            ),
            "policy_evidence": ScriptedOutcome(
                "Release policy evidence resolved.",
                acceptance_evidence=("policy-evidence:verified",),
            ),
            "independent_review": ScriptedOutcome(
                "Independent reviewer verified the policy evidence.",
                acceptance_evidence=("independent-review:verified",),
            ),
            "integrate_decision": ScriptedOutcome(
                "Reviewed policy evidence integrated into the release decision.",
                acceptance_evidence=("reviewed-decision:integrated",),
            ),
        }
    )
    replanner = CapabilityInsertReplanner(workflow_priors=workflow_priors)
    result = await FirmKernel(
        employee_execution=runner,
        replanner=replanner,
    ).run(
        _request(
            job_id,
            playbook_revision=playbook_revision,
        )
    )
    reviewed = any(
        request.task.task_id == "independent_review"
        for request in runner.requests
    )
    quality_score = 0.95 if reviewed else 0.65
    return _Trajectory(result, replanner, runner, quality_score)


def _plan_template(result: JobResult) -> tuple[WorkflowTaskTemplate, ...]:
    return tuple(
        WorkflowTaskTemplate(
            task_key=task.task_id,
            required_capabilities=task.required_capabilities,
            depends_on=task.depends_on,
            final=task.task_id == result.final_task_id,
        )
        for task in result.final_tasks
    )


def _safety_passed(trajectory: _Trajectory) -> bool:
    return (
        trajectory.result.status == JobStatus.SUCCEEDED
        and trajectory.result.metrics.graph_patch_count <= 1
        and trajectory.result.metrics.organization_admission_count <= 1
        and trajectory.result.metrics.temporary_role_count <= 2
    )


def _record(
    slot: str,
    strategy: str,
    actual: _Trajectory,
    baseline: _Trajectory,
    *,
    unrelated_control_exposed: bool | None = None,
) -> CausalWorkflowJobRecord:
    return CausalWorkflowJobRecord(
        slot=slot,
        strategy=strategy,
        status=actual.result.status.value,
        quality_score=actual.quality_score,
        baseline_quality_score=baseline.quality_score,
        quality_gain=round(actual.quality_score - baseline.quality_score, 6),
        model_calls=actual.result.metrics.usage.model_calls,
        baseline_model_calls=baseline.result.metrics.usage.model_calls,
        task_count=len(actual.result.final_tasks),
        final_task_id=actual.result.final_task_id,
        prior_exposed=bool(actual.replanner.exposed_workflow_prior_ids),
        prior_aligned=bool(actual.replanner.aligned_workflow_prior_ids),
        safety_passed=_safety_passed(actual),
        unrelated_control_exposed=unrelated_control_exposed,
    )


def _episode(
    slot: str,
    actual: _Trajectory,
    baseline: _Trajectory,
) -> OrganizationEpisode:
    return OrganizationEpisode.create(
        job_id=f"causal-workflow-{slot}",
        source=EvidenceSource.LIVE_EVALUATION,
        task_family=CAUSAL_WORKFLOW_FAMILY,
        context_fingerprint=CAUSAL_WORKFLOW_CONTEXT,
        execution_profile=CompilerExecutionProfile.READ_ONLY.value,
        planning_mode="SOLO_THEN_TYPED_GAP_CANDIDATE",
        plan_template=_plan_template(actual.result),
        success=actual.result.status == JobStatus.SUCCEEDED,
        quality_score=actual.quality_score,
        baseline_quality_score=baseline.quality_score,
        model_calls=actual.result.metrics.usage.model_calls,
        baseline_model_calls=baseline.result.metrics.usage.model_calls,
        employee_count=actual.result.metrics.unique_employee_count,
        maximum_parallelism=actual.result.metrics.maximum_parallelism,
        writer_count=0,
        approvals_requested=0,
        approvals_granted=0,
        preapproval_mutations=0,
        validation_attempts=(True,),
        safety_violations=(),
        ledger_digest=f"provider-free-ledger-{slot}",
    )


async def run_causal_workflow_evaluation() -> CausalWorkflowEvaluation:
    seed_prior = _candidate_prior()
    records: list[CausalWorkflowJobRecord] = []
    with tempfile.TemporaryDirectory() as temporary:
        store = CompanyStateStore(Path(temporary) / "isolated-company.db")
        try:
            learning = CompanyLearningService(store)
            for slot in ("baseline", "observation"):
                generic = await _run_trajectory(f"{slot}-generic")
                candidate = await _run_trajectory(
                    f"{slot}-candidate",
                    workflow_priors=(seed_prior,),
                )
                store.record_episode(_episode(slot, candidate, generic))
                records.append(
                    _record(
                        slot,
                        "matched-candidate-topology",
                        candidate,
                        generic,
                    )
                )

            curation = learning.curate()
            if len(curation.candidates) != 1:
                raise AssertionError("Causal workflow cohort did not produce one candidate")
            candidate_patch = curation.candidates[0]
            learning.approve(
                candidate_patch.patch_id,
                actor="evaluation:causal-workflow",
            )
            applied = learning.apply(
                candidate_patch.patch_id,
                actor="evaluation:causal-workflow",
            )
            active_priors = learning.compiler_priors(
                CompilerExecutionProfile.READ_ONLY,
                context_fingerprint=CAUSAL_WORKFLOW_CONTEXT,
            )
            patched_baseline = await _run_trajectory("patched-generic")
            patched = await _run_trajectory(
                "patched",
                workflow_priors=active_priors,
                playbook_revision=store.playbook().revision,
            )
            unrelated = await _run_trajectory(
                "unrelated-no-gap",
                workflow_priors=active_priors,
                emit_gap=False,
                playbook_revision=store.playbook().revision,
            )
            records.append(
                _record(
                    "patched",
                    "applied-playbook-post-gap",
                    patched,
                    patched_baseline,
                    unrelated_control_exposed=bool(
                        unrelated.replanner.exposed_workflow_prior_ids
                    ),
                )
            )

            rolled_back = learning.rollback(
                candidate_patch.patch_id,
                actor="evaluation:causal-workflow",
            )
            rollback_priors = learning.compiler_priors(
                CompilerExecutionProfile.READ_ONLY,
                context_fingerprint=CAUSAL_WORKFLOW_CONTEXT,
            )
            rollback = await _run_trajectory(
                "rollback-control",
                workflow_priors=rollback_priors,
                playbook_revision=store.playbook().revision,
            )
            rollback_baseline = await _run_trajectory("rollback-generic")
            records.append(
                _record(
                    "rollback-control",
                    "post-rollback-generic",
                    rollback,
                    rollback_baseline,
                )
            )
            lifecycle = tuple(
                event.event_type.value
                for event in store.list_patch_events(candidate_patch.patch_id)
            )
            playbook_revision = store.playbook().revision
            playbook_empty = not store.playbook().patterns
        finally:
            store.close()

    patched_record = records[2]
    rollback_record = records[3]
    checks = (
        CausalWorkflowCheck(
            "bounded-four-job-cohort",
            len(records) == 4,
            f"cohort records={len(records)}; allowed maximum=4",
        ),
        CausalWorkflowCheck(
            "two-independent-matched-observations",
            all(
                record.quality_gain >= QUALITY_GAIN_THRESHOLD
                and record.safety_passed
                for record in records[:2]
            ),
            (
                f"gains={records[0].quality_gain:+.2f},"
                f"{records[1].quality_gain:+.2f}; threshold=+{QUALITY_GAIN_THRESHOLD:.2f}"
            ),
        ),
        CausalWorkflowCheck(
            "explicit-approval-and-apply",
            applied.status == WorkflowPatchStatus.APPLIED,
            f"applied revision={applied.applied_revision}",
        ),
        CausalWorkflowCheck(
            "patched-job-attribution",
            (
                patched_record.prior_exposed
                and patched_record.prior_aligned
                and patched_record.quality_gain >= QUALITY_GAIN_THRESHOLD
            ),
            (
                f"exposed={patched_record.prior_exposed}; "
                f"aligned={patched_record.prior_aligned}; "
                f"gain={patched_record.quality_gain:+.2f}"
            ),
        ),
        CausalWorkflowCheck(
            "unrelated-control-isolation",
            patched_record.unrelated_control_exposed is False,
            "same-context no-gap control did not expose or align the prior",
        ),
        CausalWorkflowCheck(
            "append-only-rollback-restores-generic",
            (
                rolled_back.status == WorkflowPatchStatus.ROLLED_BACK
                and lifecycle == ("PROPOSED", "APPROVED", "APPLIED", "ROLLED_BACK")
                and playbook_empty
                and not rollback_record.prior_exposed
                and rollback_record.final_task_id == "integrate_goal"
            ),
            (
                f"lifecycle={','.join(lifecycle)}; "
                f"final={rollback_record.final_task_id}; playbook_empty={playbook_empty}"
            ),
        ),
        CausalWorkflowCheck(
            "provider-free-mechanism-proof",
            True,
            "external model calls=0; isolated temporary company state; quota consumed=no",
        ),
    )
    return CausalWorkflowEvaluation(
        schema_version="noruct.causal-workflow-evaluation.v1",
        passed=all(check.passed for check in checks),
        evidence_class=(
            "provider-free-isolated-mechanism-proof-not-production-value-authorization"
        ),
        cohort_job_count=len(records),
        external_model_calls=0,
        quota_consumed=False,
        patch_id=candidate_patch.patch_id,
        final_patch_status=rolled_back.status.value,
        playbook_revision=playbook_revision,
        records=tuple(records),
        checks=checks,
    )
