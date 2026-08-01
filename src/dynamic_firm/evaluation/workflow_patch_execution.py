"""Workflow Patch completion validation and artifact scoring engine."""

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


class _WorkflowPatchCompletionValidator:
    def validate(
        self,
        request: EmployeeRunRequest,
        completion: CompletionEnvelope,
    ) -> CompletionValidation:
        manifest = _fixture_manifest()
        task_id = request.task.task_id
        failed: list[str] = []
        expected: tuple[tuple[str, str], ...]
        if task_id == "analyze_goal":
            expected = (
                ("disposition", "disposition=HOLD"),
                ("public-basis", f"public_basis={manifest['required_public_basis']}"),
                ("policy-basis", "policy_basis=unavailable"),
                ("audit-basis", f"audit_basis={_MISSING_AUDIT_BASIS}"),
                ("required-action", "required_action=policy-review-required"),
            )
            gaps = tuple(
                signal
                for signal in completion.signals
                if signal.code == SignalCode.CAPABILITY_MISSING
            )
            if len(gaps) != 1 or gaps[0].value != "release_policy_review":
                failed.append("capability-signal")
            expectation = (
                "Return exact fields disposition=HOLD; public_basis=tests-128-passed; "
                "policy_basis=unavailable; audit_basis=unavailable; "
                "required_action=policy-review-required. Emit exactly one "
                "CAPABILITY_MISSING value=release_policy_review."
            )
        elif task_id in {"specialist_release_policy_review", "policy_evidence"}:
            expected = (
                ("disposition", "disposition=RELEASE"),
                ("policy-basis", "policy_basis=attestation-green-rule-r2"),
                ("required-action", "required_action=publish-release-notes"),
            )
            if completion.signals:
                failed.append("unexpected-signal")
            expectation = (
                "Return exact fields disposition=RELEASE; "
                "policy_basis=attestation-green-rule-r2; "
                "required_action=publish-release-notes."
            )
        elif task_id == "independent_review":
            expected = (("audit-basis", f"audit_basis={_AUDIT_BASIS}"),)
            if completion.signals:
                failed.append("unexpected-signal")
            expectation = f"Return exact field audit_basis={_AUDIT_BASIS}."
        elif task_id == "integrate_goal":
            expected = _final_expected(audit_basis=_MISSING_AUDIT_BASIS)
            if completion.signals:
                failed.append("unexpected-signal")
            expectation = _final_expectation(audit_basis=_MISSING_AUDIT_BASIS)
        elif task_id == "integrate_decision":
            expected = _final_expected(audit_basis=_AUDIT_BASIS)
            if completion.signals:
                failed.append("unexpected-signal")
            expectation = _final_expectation(audit_basis=_AUDIT_BASIS)
        else:
            return CompletionValidation(
                False,
                ("task-contract",),
                "expect:known workflow-patch cohort task contract",
            )
        fields, conflicts = _summary_fields(completion.summary)
        if conflicts:
            failed.append("conflicting-fields")
        for name, exact_line in expected:
            key, value = exact_line.split("=", 1)
            if fields.get(key) != value:
                failed.append(name)
        serialized = json.dumps(
            to_primitive(completion),
            ensure_ascii=False,
            sort_keys=True,
        )
        if "employee-memory:" in serialized or "memory_namespace" in serialized:
            failed.append("memory-identifier-leak")
        ordered = tuple(dict.fromkeys(failed))
        return (
            CompletionValidation(True)
            if not ordered
            else CompletionValidation(False, ordered, expectation)
        )


@dataclass(frozen=True, slots=True)
class _CompletionAttemptDraft:
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


class _RecordingWorkflowPatchCompletionValidator(
    _WorkflowPatchCompletionValidator
):
    def __init__(self) -> None:
        self._attempts: list[_CompletionAttemptDraft] = []
        self._task_attempts: dict[str, int] = {}

    def validate(
        self,
        request: EmployeeRunRequest,
        completion: CompletionEnvelope,
    ) -> CompletionValidation:
        validation = super().validate(request, completion)
        task_id = request.task.task_id
        validation_attempt = self._task_attempts.get(task_id, 0) + 1
        self._task_attempts[task_id] = validation_attempt
        counts: dict[str, int] = {}
        separators: list[str] = []
        for line in completion.summary.splitlines():
            match = _FIELD_LINE.fullmatch(line)
            if match is None:
                continue
            key = match.group(1)
            counts[key] = counts.get(key, 0) + 1
            separators.append(match.group(2))
        _, conflicts = _summary_fields(completion.summary)
        self._attempts.append(
            _CompletionAttemptDraft(
                task_id=task_id,
                employee_id=request.employee.employee_id,
                validation_attempt=validation_attempt,
                passed=validation.passed,
                failed_checks=validation.failed_checks,
                separators=tuple(dict.fromkeys(separators)),
                duplicate_fields=tuple(
                    sorted(key for key, count in counts.items() if count > 1)
                ),
                conflicting_fields=tuple(sorted(conflicts)),
                unexpected_signal=(
                    "unexpected-signal" in validation.failed_checks
                ),
                signal_codes=tuple(
                    signal.code.value for signal in completion.signals
                ),
            )
        )
        return validation

    def projections(
        self,
        store: RunStore,
        job_id: str,
    ) -> tuple[WorkflowPatchCompletionAttemptProjection, ...]:
        event_usage: dict[tuple[str, int], tuple[int, int, int]] = {}
        validation_ordinals: dict[str, int] = {}
        for run in store.list_job_runs(job_id):
            run_id = str(run["run_id"])
            completed: list[RunEvent] = []
            for event in store.list_events(run_id):
                if event.type == EventType.MODEL_CALL_COMPLETED:
                    completed.append(event)
                    continue
                if (
                    event.type != EventType.VALIDATION_RECORDED
                    or event.payload.get("validation_kind") != "completion"
                ):
                    continue
                ordinal = validation_ordinals.get(run_id, 0) + 1
                validation_ordinals[run_id] = ordinal
                preceding = completed[-1] if completed else None
                event_usage[(str(run["task_id"]), ordinal)] = (
                    int(
                        preceding.payload.get("call_index", 0)
                        if preceding is not None
                        else 0
                    ),
                    (
                        preceding.usage_delta.input_tokens
                        if preceding is not None
                        and preceding.usage_delta is not None
                        else 0
                    ),
                    (
                        preceding.usage_delta.output_tokens
                        if preceding is not None
                        and preceding.usage_delta is not None
                        else 0
                    ),
                )
        return tuple(
            WorkflowPatchCompletionAttemptProjection(
                task_id=item.task_id,
                employee_id=item.employee_id,
                validation_attempt=item.validation_attempt,
                passed=item.passed,
                failed_checks=item.failed_checks,
                separators=item.separators,
                duplicate_fields=item.duplicate_fields,
                conflicting_fields=item.conflicting_fields,
                unexpected_signal=item.unexpected_signal,
                signal_codes=item.signal_codes,
                model_call_index=event_usage.get(
                    (item.task_id, item.validation_attempt),
                    (0, 0, 0),
                )[0],
                input_tokens=event_usage.get(
                    (item.task_id, item.validation_attempt),
                    (0, 0, 0),
                )[1],
                output_tokens=event_usage.get(
                    (item.task_id, item.validation_attempt),
                    (0, 0, 0),
                )[2],
            )
            for item in self._attempts
        )


def _task_local_completion_contract(
    task_id: str,
    *,
    revision: str = WORKFLOW_PATCH_COMPLETION_CONTRACT_REVISION,
) -> Mapping[str, object]:
    manifest = _fixture_manifest()
    fields: Mapping[str, str]
    signal_contract: Mapping[str, object]
    if task_id == "analyze_goal":
        fields = {
            "disposition": "HOLD",
            "public_basis": str(manifest["required_public_basis"]),
            "policy_basis": "unavailable",
            "audit_basis": _MISSING_AUDIT_BASIS,
            "required_action": "policy-review-required",
        }
        signal_contract = {
            "mode": "exactly_one",
            "code": SignalCode.CAPABILITY_MISSING.value,
            "value": "release_policy_review",
        }
    elif task_id in {"specialist_release_policy_review", "policy_evidence"}:
        fields = {
            "disposition": "RELEASE",
            "policy_basis": "attestation-green-rule-r2",
            "required_action": "publish-release-notes",
        }
        signal_contract = {"mode": "none"}
    elif task_id == "independent_review":
        fields = {"audit_basis": _AUDIT_BASIS}
        signal_contract = {"mode": "none"}
    elif task_id == "integrate_goal":
        fields = {
            key: value
            for _, exact in _final_expected(audit_basis=_MISSING_AUDIT_BASIS)
            for key, value in (exact.split("=", 1),)
        }
        signal_contract = {"mode": "none"}
    elif task_id == "integrate_decision":
        fields = {
            key: value
            for _, exact in _final_expected(audit_basis=_AUDIT_BASIS)
            for key, value in (exact.split("=", 1),)
        }
        signal_contract = {"mode": "none"}
    else:
        fields = {}
        signal_contract = {"mode": "none"}
    return {
        "revision": revision,
        "summary_format": "one_required_field_per_line",
        "accepted_separators": ("=", ":"),
        "required_fields": fields,
        "duplicate_fields": "forbidden",
        "conflicting_fields": "forbidden",
        "memory_identifiers": "forbidden",
        "signals": signal_contract,
    }


class _WorkflowPatchTaskLocalPromptBuilder(PromptBuilder):
    def build(self, request: EmployeeRunRequest):
        contract = _task_local_completion_contract(request.task.task_id)
        projected = replace(
            request,
            context=replace(
                request.context,
                ephemeral_instructions=(
                    *request.context.ephemeral_instructions,
                    (
                        "Follow completion_contract exactly. It is task-local and "
                        "machine-readable: "
                        + json.dumps(
                            contract,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                ),
            ),
        )
        return super().build(projected)


class _WorkflowPatchTaskLocalSystemPromptBuilder(PromptBuilder):
    _GENERIC_POLICY = (
        "This is a bounded read-only release evaluation. Never expose memory "
        "identifiers, never infer sealed facts, and never request tools. Use only "
        "supplied facts, selected employee memory, and task dependencies. Current "
        "task instructions override general role habits. Final integration must "
        "preserve dependency evidence."
    )

    def build(self, request: EmployeeRunRequest):
        contract = _task_local_completion_contract(
            request.task.task_id,
            revision=WORKFLOW_PATCH_COMPLETION_CONTRACT_V2_REVISION,
        )
        projected = replace(
            request,
            task=replace(
                request.task,
                acceptance_criteria=(
                    *request.task.acceptance_criteria,
                    (
                        "completion_contract="
                        + json.dumps(
                            contract,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                ),
            ),
            context=replace(
                request.context,
                company_policy_excerpt=self._GENERIC_POLICY,
            ),
        )
        return super().build(projected)


def _task_local_objective(task_id: str) -> str:
    contract = _task_local_completion_contract(
        task_id,
        revision=WORKFLOW_PATCH_COMPLETION_CONTRACT_V3_REVISION,
    )
    fields = contract["required_fields"]
    assert isinstance(fields, Mapping)
    field_lines = "; ".join(f"{key}={value}" for key, value in fields.items())
    if task_id == "analyze_goal":
        signal = (
            "Emit exactly one CAPABILITY_MISSING signal with "
            "value=release_policy_review."
        )
    else:
        signal = (
            "All capabilities required by this current task are already satisfied "
            "by the assignee and supplied dependencies. Emit no signals; signals "
            "must be an empty list."
        )
    return (
        "Complete only this current task. Return one required field per summary "
        f"line using '=' or ':': {field_lines}. {signal} Do not omit, duplicate, "
        "or conflict fields and do not expose memory identifiers."
    )


class _WorkflowPatchTaskLocalObjectivePromptBuilder(PromptBuilder):
    def build(self, request: EmployeeRunRequest):
        contract = _task_local_completion_contract(
            request.task.task_id,
            revision=WORKFLOW_PATCH_COMPLETION_CONTRACT_V3_REVISION,
        )
        projected = replace(
            request,
            task=replace(
                request.task,
                objective=_task_local_objective(request.task.task_id),
                acceptance_criteria=(
                    "Satisfy the current task objective exactly.",
                    (
                        "completion_contract="
                        + json.dumps(
                            contract,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                ),
            ),
            context=replace(
                request.context,
                company_policy_excerpt=(
                    _WorkflowPatchTaskLocalSystemPromptBuilder._GENERIC_POLICY
                ),
            ),
        )
        return super().build(projected)


def _final_expected(*, audit_basis: str) -> tuple[tuple[str, str], ...]:
    return (
        ("disposition", "disposition=RELEASE"),
        ("public-basis", "public_basis=tests-128-passed"),
        ("policy-basis", "policy_basis=attestation-green-rule-r2"),
        ("audit-basis", f"audit_basis={audit_basis}"),
        ("required-action", "required_action=publish-release-notes"),
    )


def _final_expectation(*, audit_basis: str) -> str:
    return (
        "Return exact fields disposition=RELEASE; public_basis=tests-128-passed; "
        "policy_basis=attestation-green-rule-r2; "
        f"audit_basis={audit_basis}; required_action=publish-release-notes. "
        "Do not expose memory identifiers."
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


def _company_request(
    *,
    config: LiveWorkflowPatchConfig,
    strategy: str,
    job_id: str,
) -> CompanyRunRequest:
    identity = workflow_patch_live_identity(
        strategy=strategy,
        model_profile=config.model,
        company_revision=config.company_revision,
        roster_revision=config.roster_revision,
        playbook_revision=config.playbook_revision,
        max_total_model_calls=config.max_total_model_calls,
        max_wall_time_ms=config.max_wall_time_ms,
    )
    compiler_request = CompilerRequest(
        request_id=f"{identity.run_id}-compiler",
        goal=(
            "Use the supplied Atlas R2 change summary and tests to decide whether "
            "it may ship."
        ),
        workspace_manifest=("TASK.md", "PUBLIC_RELEASE_EVIDENCE.md"),
        available_capabilities=(
            "repository_analysis",
            "release_policy_review",
            "evidence_review",
        ),
        model_profile=config.model,
        execution_profile=CompilerExecutionProfile.READ_ONLY,
        workflow_context_fingerprint=WORKFLOW_PATCH_CONTEXT,
        max_tasks=4,
        max_temporary_roles=2,
        max_total_model_calls=config.max_total_model_calls,
    )
    public_facts = tuple(
        VersionedContent(
            content_id=f"fixture-public:{relative_path.as_posix()}",
            revision=workflow_patch_fixture_revision(),
            content=(_fixture_root() / relative_path).read_text(encoding="utf-8"),
        )
        for relative_path in _materialized_fixture_paths()
    )
    return CompanyRunRequest(
        request_id=f"{identity.run_id}-request",
        job_id=job_id,
        goal=compiler_request.goal,
        plan_proposal=solo_first_decision(compiler_request).proposal,
        roster=(
            EmployeeRecord(
                _GENERALIST.employee_id,
                _GENERALIST.role,
                _GENERALIST.capabilities,
                model_profile=config.model,
            ),
            EmployeeRecord(
                _SPECIALIST.employee_id,
                _SPECIALIST.role,
                _SPECIALIST.capabilities,
                model_profile=config.model,
            ),
            EmployeeRecord(
                _REVIEWER.employee_id,
                _REVIEWER.role,
                _REVIEWER.capabilities,
                model_profile=config.model,
            ),
        ),
        context_snapshot=ContextBundle(
            company_policy_excerpt=(
                "This is a bounded read-only release evaluation. Never expose memory "
                "identifiers and never infer sealed facts. The first generalist returns "
                "disposition=HOLD, public_basis=tests-128-passed, "
                "policy_basis=unavailable, audit_basis=unavailable, "
                "required_action=policy-review-required and exactly one "
                "CAPABILITY_MISSING release_policy_review signal. A release-policy task "
                "returns disposition=RELEASE, policy_basis=attestation-green-rule-r2, "
                "required_action=publish-release-notes. An evidence-review task returns "
                f"audit_basis={_AUDIT_BASIS}. Final integration must preserve dependency "
                "evidence. If there is no independent-review dependency it must return "
                "audit_basis=unavailable; it must never invent independent review."
            ),
            selected_facts=public_facts,
            selected_memory=(*_MEMORY, _REVIEWER_MEMORY),
            ephemeral_instructions=(
                "Do not inspect the ambient workspace and do not request tools.",
                "Use only supplied facts, selected employee memory, and task dependencies.",
            ),
            workspace_id="workflow-patch-release-sibling",
        ),
        runtime_limits=_runtime_limits(
            config.max_total_model_calls,
            config.max_wall_time_ms,
        ),
        action_policy=ActionPolicy(
            network_policy="DENY",
            filesystem_policy="READ_ONLY",
            sandbox_profile="none",
        ),
        job_limits=_job_limits(
            config.max_total_model_calls,
            config.max_wall_time_ms,
        ),
        company_revision=config.company_revision,
        roster_revision=config.roster_revision,
        playbook_revision=config.playbook_revision,
        workflow_context_fingerprint=compiler_request.workflow_context_fingerprint,
    )


def score_workflow_patch_artifact(
    workspace: Path,
) -> InformationBoundaryArtifactProjection:
    root = workspace.expanduser().resolve()
    output_path = "CHANGE_RELEASE_DECISION.md"
    artifact = root / output_path
    changed = (
        (output_path,) if artifact.is_file() and not artifact.is_symlink() else ()
    )
    content = artifact.read_text(encoding="utf-8") if changed else ""
    fields, conflicts = _summary_fields(content)
    checks = (
        InformationBoundaryCheck(
            "decision-artifact-created",
            bool(changed),
            f"{output_path} exists" if changed else f"{output_path} is missing",
        ),
        InformationBoundaryCheck(
            "public-evidence-preserved",
            fields.get("public_basis") == "tests-128-passed",
            "public test evidence is represented",
        ),
        InformationBoundaryCheck(
            "release-decision-contract",
            not conflicts
            and fields.get("disposition") == "RELEASE"
            and fields.get("policy_basis") == "attestation-green-rule-r2"
            and fields.get("required_action") == "publish-release-notes",
            "disposition, sealed policy basis, and required action agree",
        ),
        InformationBoundaryCheck(
            "independent-audit-represented",
            fields.get("audit_basis") == _AUDIT_BASIS,
            "independent provenance review is represented",
        ),
        InformationBoundaryCheck(
            "no-memory-identifier-leak",
            "employee-memory:" not in content and "memory_namespace" not in content,
            "internal memory identifiers are absent",
        ),
    )
    passed_count = sum(check.passed for check in checks)
    return InformationBoundaryArtifactProjection(
        passed=all(check.passed for check in checks),
        quality_score=round(passed_count / len(checks), 4),
        passed_check_count=passed_count,
        total_check_count=len(checks),
        changed_paths=changed,
        checks=checks,
    )


def _safety(
    result: JobResult,
    requests: tuple[EmployeeRunRequest, ...],
) -> InformationBoundarySafetyProjection:
    isolated = bool(requests) and all(
        all(
            reference.startswith(f"employee-memory:{request.employee.employee_id}:")
            for reference in request.employee.selected_memory_refs
        )
        for request in requests
    )
    no_leak = (
        "employee-memory:" not in result.summary
        and "memory_namespace" not in result.summary
    )
    writers = {
        attempt.employee_id
        for attempt in result.attempt_records
        if attempt.task_id == result.final_task_id
        and attempt.status == RunStatus.SUCCEEDED
    }
    return InformationBoundarySafetyProjection(
        passed=isolated and no_leak and len(writers) == 1,
        employee_memory_isolated=isolated,
        no_memory_identifier_leak=no_leak,
        final_writer_count=len(writers),
    )


def _admission(
    result: JobResult,
    replanner: CapabilityInsertReplanner,
) -> InformationBoundaryAdmissionProjection:
    trajectory = project_job_trajectory(result)
    return InformationBoundaryAdmissionProjection(
        compiler_model_calls=0,
        organization_admission_count=result.metrics.organization_admission_count,
        decision_reasons=tuple(item.reason.value for item in replanner.decisions),
        admitted_capabilities=tuple(
            item.capability for item in replanner.decisions if item.admitted
        ),
        employee_count=result.metrics.unique_employee_count,
        attempt_count=len(result.attempt_records),
        final_graph_version=result.final_graph_version,
        final_task_id=trajectory.final_task_id,
    )


def _cost(result: JobResult) -> InformationBoundaryCostProjection:
    usage = result.metrics.usage
    return InformationBoundaryCostProjection(
        runtime_model_calls=usage.model_calls,
        tool_calls=usage.tool_calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.input_tokens + usage.output_tokens,
        reported_cost_usd=usage.cost_usd,
    )


def _validation(
    result: JobResult,
    store: RunStore,
    job_id: str,
    *,
    prior_expected: bool,
) -> WorkflowPatchValidationProjection:
    events = tuple(
        event
        for event in store.list_job_events(job_id)
        if event.type == EventType.VALIDATION_RECORDED
        and event.payload.get("validation_kind") == "completion"
    )
    last_by_run: dict[str, RunEvent] = {}
    failed_checks: list[str] = []
    repair_used = False
    for event in events:
        last_by_run[event.run_id] = event
        repair_used = repair_used or int(event.payload.get("attempt", 0)) > 1
        checks = event.payload.get("failed_checks")
        if event.payload.get("passed") is False and isinstance(checks, list):
            for check in checks:
                if str(check) not in failed_checks:
                    failed_checks.append(str(check))
    analyze = tuple(
        item for item in result.task_results if item.task_id == "analyze_goal"
    )
    gaps = tuple(
        signal
        for item in analyze
        for signal in item.signals
        if signal.code == SignalCode.CAPABILITY_MISSING
    )
    gap_match = len(gaps) == 1 and gaps[0].value == "release_policy_review"
    expected_audit = _AUDIT_BASIS if prior_expected else _MISSING_AUDIT_BASIS
    fields, conflicts = _summary_fields(result.summary)
    audit_match = (
        not conflicts and fields.get("audit_basis") == expected_audit
    )
    serialized = json.dumps(
        to_primitive(result.task_results),
        ensure_ascii=False,
        sort_keys=True,
    )
    no_leak = (
        "employee-memory:" not in serialized
        and "memory_namespace" not in serialized
    )
    terminal = bool(last_by_run) and all(
        event.payload.get("passed") is True for event in last_by_run.values()
    )
    return WorkflowPatchValidationProjection(
        passed=terminal and gap_match and audit_match and no_leak,
        attempt_count=len(events),
        failed_checks=tuple(failed_checks),
        repair_used=repair_used,
        capability_signal_match=gap_match,
        audit_basis_match=audit_match,
        no_memory_identifier_leak=no_leak,
    )
