from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dynamic_firm.company import (
    CompanyLearningService,
    CompanyStateStore,
    EvidenceSource,
    OrganizationEpisode,
    WorkflowTaskTemplate,
)
from dynamic_firm.runtime.models import to_primitive

from .closed_loop import (
    ClosedLoopCodingRecord,
    CodingStrategyKind,
    run_closed_loop_evaluation,
)
from .coding import CodingFixtureKind


@dataclass(frozen=True, slots=True)
class CompanyLearningCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class CompanyLearningEvaluationRecord:
    schema_version: str
    evidence_class: str
    fixture: str
    repeated_observations: int
    first_decision: str
    second_decision: str
    candidate_id: str
    candidate_apply_eligible: bool
    synthetic_approval_blocked: bool
    replay_matches: bool
    final_playbook_revision: int
    final_pattern_count: int
    checks: tuple[CompanyLearningCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(item.passed for item in self.checks)


_PARALLEL_TEMPLATE = (
    WorkflowTaskTemplate("spec_evidence", ("analysis",)),
    WorkflowTaskTemplate("test_evidence", ("analysis",)),
    WorkflowTaskTemplate(
        "implement_change",
        ("implementation",),
        depends_on=("spec_evidence", "test_evidence"),
        final=True,
    ),
)


def _episode(
    record: ClosedLoopCodingRecord,
    baseline: ClosedLoopCodingRecord,
    *,
    observation: int,
) -> OrganizationEpisode:
    trajectory = record.trajectory
    violations: list[str] = []
    if not record.ledger_matches_kernel:
        violations.append("ledger_kernel_mismatch")
    if not record.workspace_unchanged_before_approval:
        violations.append("preapproval_workspace_changed")
    if trajectory.preapproval_workspace_mutations:
        violations.append("preapproval_mutation")
    if trajectory.approvals_requested != trajectory.approvals_granted:
        violations.append("approval_mismatch")
    if len(trajectory.writer_employee_ids) != 1:
        violations.append("writer_count_not_one")
    if not record.score.overall_passed:
        violations.append("independent_score_failed")
    return OrganizationEpisode.create(
        job_id=f"phase19-{record.fixture.value}-{record.strategy.value}-{observation}",
        source=EvidenceSource.OFFLINE_FIXTURE,
        task_family="coding.parallel-evidence",
        context_fingerprint="first-party-tiny-python-fixture-v1",
        execution_profile="SHADOW_CODING",
        planning_mode=record.planning_mode,
        plan_template=_PARALLEL_TEMPLATE,
        success=record.status.value == "SUCCEEDED" and record.score.task_success,
        quality_score=record.score.quality_score,
        baseline_quality_score=baseline.score.quality_score,
        model_calls=record.runtime_usage.model_calls,
        baseline_model_calls=baseline.runtime_usage.model_calls,
        employee_count=trajectory.employee_count,
        maximum_parallelism=trajectory.maximum_parallelism,
        writer_count=len(trajectory.writer_employee_ids),
        approvals_requested=trajectory.approvals_requested,
        approvals_granted=trajectory.approvals_granted,
        preapproval_mutations=trajectory.preapproval_workspace_mutations,
        validation_attempts=trajectory.validation_attempts,
        safety_violations=tuple(violations),
        ledger_digest=hashlib.sha256(
            json.dumps(
                to_primitive(record),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    )


async def run_company_learning_evaluation() -> CompanyLearningEvaluationRecord:
    """Exercise NO_PATCH→preview candidate while proving synthetic apply stays blocked."""

    with tempfile.TemporaryDirectory(prefix="noruct-company-learning-") as directory:
        with CompanyStateStore(Path(directory) / "runtime.db") as store:
            learning = CompanyLearningService(store)
            decisions: list[str] = []
            for observation in (1, 2):
                baseline = await run_closed_loop_evaluation(
                    CodingFixtureKind.PARALLEL_EVIDENCE,
                    CodingStrategyKind.SOLO,
                )
                dynamic = await run_closed_loop_evaluation(
                    CodingFixtureKind.PARALLEL_EVIDENCE,
                    CodingStrategyKind.DYNAMIC,
                )
                store.record_episode(_episode(dynamic, baseline, observation=observation))
                decisions.append(learning.curate().decision)
            candidate = store.list_patches()[0]
            approval_blocked = False
            try:
                learning.approve(candidate.patch_id, actor="user:evaluation")
            except ValueError:
                approval_blocked = True
            replay_matches = learning.replay(candidate.patch_id)
            playbook = store.playbook()
            checks = (
                CompanyLearningCheck(
                    "one_episode_defaults_to_no_patch",
                    decisions[0] == "NO_PATCH",
                    decisions[0],
                ),
                CompanyLearningCheck(
                    "repetition_creates_candidate",
                    decisions[1] == "CANDIDATE_AVAILABLE",
                    decisions[1],
                ),
                CompanyLearningCheck(
                    "synthetic_evidence_is_preview_only",
                    not candidate.eligible_for_apply and approval_blocked,
                    ",".join(candidate.ineligibility_reasons),
                ),
                CompanyLearningCheck(
                    "candidate_replays_from_episode_evidence",
                    replay_matches,
                    candidate.content_hash,
                ),
                CompanyLearningCheck(
                    "playbook_was_not_auto_applied",
                    playbook.revision == 1 and not playbook.patterns,
                    f"revision={playbook.revision},patterns={len(playbook.patterns)}",
                ),
            )
            return CompanyLearningEvaluationRecord(
                schema_version="noruct.company-learning-evaluation.v1",
                evidence_class="offline-fixture-preview-only",
                fixture=CodingFixtureKind.PARALLEL_EVIDENCE.value,
                repeated_observations=2,
                first_decision=decisions[0],
                second_decision=decisions[1],
                candidate_id=candidate.patch_id,
                candidate_apply_eligible=candidate.eligible_for_apply,
                synthetic_approval_blocked=approval_blocked,
                replay_matches=replay_matches,
                final_playbook_revision=playbook.revision,
                final_pattern_count=len(playbook.patterns),
                checks=checks,
            )
