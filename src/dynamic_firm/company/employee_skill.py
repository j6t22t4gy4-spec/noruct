from __future__ import annotations

import json
import re
from typing import Mapping, Sequence

from dynamic_firm.runtime.models import VersionedContent, utc_now
from dynamic_firm.runtime.knowledge_retrieval import BoundedKnowledgeRetriever

from .models import (
    EmployeeSkillAssessment,
    EmployeeSkillAssessmentDecision,
    EmployeeSkillEvidence,
    EmployeeSkillEvidenceKind,
    EmployeeSkillObservation,
    EmployeeSkillPatchCandidate,
    EmployeeSkillPatchStatus,
    EmployeeSkillProcedure,
    EvidenceSource,
    OrganizationEpisode,
    canonical_json,
    content_digest,
)
from .store import CompanyStateStore


_SKILL_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CONTEXT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_BLOCKED_CONTENT = (
    "ignore previous",
    "ignore all previous",
    "system prompt",
    "bypass approval",
    "disable approval",
    "override company",
    "override policy",
    "escalate privileges",
    "```",
    "#!",
)


def _text(value: str, field: str, *, maximum: int) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise ValueError(f"Employee Skill {field} must be non-empty")
    if len(normalized) > maximum:
        raise ValueError(f"Employee Skill {field} exceeds {maximum} characters")
    if "\x00" in normalized:
        raise ValueError(f"Employee Skill {field} contains a NUL byte")
    return normalized


def _lines(
    values: Sequence[str],
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> tuple[str, ...]:
    normalized = tuple(_text(item, field, maximum=240) for item in values)
    if not minimum <= len(normalized) <= maximum:
        raise ValueError(
            f"Employee Skill {field} requires {minimum}..{maximum} entries"
        )
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"Employee Skill {field} entries must be unique")
    return normalized


def validate_employee_skill_procedure(
    procedure: EmployeeSkillProcedure,
) -> EmployeeSkillProcedure:
    employee_id = _text(procedure.employee_id, "employee_id", maximum=96)
    skill_key = procedure.skill_key.strip()
    context_key = procedure.context_key.strip()
    if not _SKILL_KEY.fullmatch(skill_key):
        raise ValueError(
            "Employee Skill skill_key must be lowercase and use letters, numbers, '.', '_', or '-'"
        )
    if not _CONTEXT_KEY.fullmatch(context_key):
        raise ValueError("Employee Skill context_key is not a bounded exact key")
    if procedure.authority_scope != "INHERIT_ONLY":
        raise ValueError("Employee Skill cannot override COMPANY or ActionPolicy authority")
    if procedure.workflow_scope != "INHERIT_ONLY":
        raise ValueError("Employee Skill cannot override PLAYBOOK workflow constraints")
    normalized = EmployeeSkillProcedure(
        employee_id=employee_id,
        skill_key=skill_key,
        context_key=context_key,
        purpose=_text(procedure.purpose, "purpose", maximum=240),
        steps=_lines(procedure.steps, "step", minimum=1, maximum=8),
        verification_steps=_lines(
            procedure.verification_steps,
            "verification step",
            minimum=1,
            maximum=4,
        ),
        prohibitions=_lines(
            procedure.prohibitions,
            "prohibition",
            minimum=0,
            maximum=4,
        ),
        authority_scope="INHERIT_ONLY",
        workflow_scope="INHERIT_ONLY",
    )
    rendered = canonical_json(normalized)
    if len(rendered.encode("utf-8")) > 4_096:
        raise ValueError("Employee Skill procedure exceeds 4096 UTF-8 bytes")
    lowered = rendered.casefold()
    blocked = next((item for item in _BLOCKED_CONTENT if item in lowered), None)
    if blocked is not None:
        raise ValueError(
            f"Employee Skill contains blocked authority or executable content: {blocked}"
        )
    return normalized


def employee_skill_observation_from_runtime_ledger(
    episode: OrganizationEpisode,
    runs: Sequence[Mapping[str, object]],
    *,
    contract,
    existing_cohort_count: int,
) -> EmployeeSkillObservation:
    expected_content_id = (
        f"employee-skill:{contract.employee_id}:{contract.skill_key}:{contract.context_key}"
    )
    relevant_requests: list[Mapping[str, object]] = []
    skill_exposed = False
    for row in runs:
        if str(row.get("job_id", "")) != episode.job_id:
            raise ValueError("Employee Skill observation contains a different job")
        raw = row.get("request_json")
        if not isinstance(raw, str):
            raise ValueError("Employee Skill observation requires immutable request JSON")
        try:
            request = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Employee Skill observation request is malformed") from exc
        if not isinstance(request, Mapping):
            raise ValueError("Employee Skill observation request must be an object")
        employee = request.get("employee")
        if not isinstance(employee, Mapping):
            raise ValueError("Employee Skill observation lacks employee snapshot")
        if str(employee.get("employee_id", "")) != contract.employee_id:
            continue
        relevant_requests.append(request)
        skills = employee.get("skills", ())
        if not isinstance(skills, (list, tuple)):
            raise ValueError("Employee Skill snapshot must be a sequence")
        for skill in skills:
            if not isinstance(skill, Mapping):
                raise ValueError("Employee Skill snapshot entry must be an object")
            if (
                str(skill.get("content_id", "")) == expected_content_id
                and str(skill.get("revision", ""))
                == str(contract.applied_skill_revision)
                and str(skill.get("content_hash", "")) == contract.version_content_hash
            ):
                skill_exposed = True

    reasons: list[str] = []
    if not episode.production_eligible:
        reasons.append("non_production_evidence")
    if episode.context_fingerprint != contract.context_key:
        reasons.append("context_key_mismatch")
    if not relevant_requests:
        reasons.append("employee_not_assigned")
    if relevant_requests and not skill_exposed:
        reasons.append("exact_skill_version_not_exposed")
    attribution_eligible = not reasons
    if attribution_eligible and existing_cohort_count >= contract.maximum_observations:
        reasons.append("observation_limit_reached")
    cohort_eligible = attribution_eligible and not reasons
    request_projection = tuple(
        content_digest(request) for request in relevant_requests
    )
    immutable = {
        "patch_id": contract.patch_id,
        "episode_id": episode.episode_id,
        "job_id": episode.job_id,
        "skill_exposed": skill_exposed,
        "attribution_eligible": attribution_eligible,
        "cohort_eligible": cohort_eligible,
        "ineligibility_reasons": tuple(reasons),
        "job_succeeded": episode.success,
        "validation_attempts": episode.validation_attempts,
        "safety_violations": episode.safety_violations,
        "writer_count": episode.writer_count,
        "approvals_requested": episode.approvals_requested,
        "approvals_granted": episode.approvals_granted,
        "preapproval_mutations": episode.preapproval_mutations,
        "request_ledger_digest": content_digest(request_projection),
    }
    digest = content_digest(immutable)
    return EmployeeSkillObservation(
        observation_id=f"employee-skill-observation-{digest[:24]}",
        **immutable,
        content_hash=digest,
        recorded_at=episode.recorded_at,
    )


class EmployeeSkillPatchService:
    """Evidence-gated data-only employee procedure lifecycle."""

    def __init__(self, store: CompanyStateStore) -> None:
        self.store = store

    def _ensure_employee(self, employee_id: str) -> None:
        matches = tuple(
            item
            for item in self.store.roster().employees
            if item.get("employee_id") == employee_id and item.get("active") is True
        )
        if len(matches) != 1:
            raise ValueError("Employee Skill requires exactly one active persistent employee")
        if matches[0].get("temporary") is True:
            raise ValueError("Employee Skill cannot target a temporary employee")

    @staticmethod
    def _evidence(
        procedure: EmployeeSkillProcedure,
        *,
        kind: EmployeeSkillEvidenceKind,
        source_ref: str,
        source: EvidenceSource,
        confirmed_by_user: bool,
        job_succeeded: bool,
        validation_passed: bool,
        safety_passed: bool,
    ) -> EmployeeSkillEvidence:
        immutable = {
            "kind": kind,
            "source_ref": _text(source_ref, "source_ref", maximum=160),
            "source": source,
            "employee_id": procedure.employee_id,
            "skill_key": procedure.skill_key,
            "context_key": procedure.context_key,
            "procedure_hash": content_digest(procedure.content_payload()),
            "confirmed_by_user": confirmed_by_user,
            "job_succeeded": job_succeeded,
            "validation_passed": validation_passed,
            "safety_passed": safety_passed,
        }
        digest = content_digest(immutable)
        return EmployeeSkillEvidence(
            evidence_id=f"employee-skill-evidence-{digest[:24]}",
            **immutable,
            content_hash=digest,
            recorded_at=utc_now().isoformat(),
        )

    def record_verified_job_procedure(
        self,
        procedure: EmployeeSkillProcedure,
        *,
        episode_id: str,
    ) -> EmployeeSkillEvidence:
        procedure = validate_employee_skill_procedure(procedure)
        self._ensure_employee(procedure.employee_id)
        episode = self.store.get_episode(episode_id)
        if not episode.production_eligible:
            raise ValueError("Employee Skill job evidence must be production eligible")
        if episode.context_fingerprint != procedure.context_key:
            raise ValueError("Employee Skill job evidence context differs")
        evidence = self._evidence(
            procedure,
            kind=EmployeeSkillEvidenceKind.VERIFIED_JOB_PROCEDURE,
            source_ref=episode.episode_id,
            source=episode.source,
            confirmed_by_user=False,
            job_succeeded=episode.success,
            validation_passed=bool(episode.validation_attempts)
            and all(episode.validation_attempts),
            safety_passed=episode.safety_passed,
        )
        return self.store.record_employee_skill_evidence(evidence)[0]

    def propose_user_correction(
        self,
        procedure: EmployeeSkillProcedure,
        *,
        correction_id: str,
        rationale: str,
        actor: str,
    ) -> EmployeeSkillPatchCandidate:
        procedure = validate_employee_skill_procedure(procedure)
        self._ensure_employee(procedure.employee_id)
        evidence = self._evidence(
            procedure,
            kind=EmployeeSkillEvidenceKind.USER_CORRECTION,
            source_ref=correction_id,
            source=EvidenceSource.USER_CORRECTION,
            confirmed_by_user=True,
            job_succeeded=True,
            validation_passed=True,
            safety_passed=True,
        )
        stored = self.store.record_employee_skill_evidence(evidence)[0]
        return self.propose_from_evidence(
            procedure,
            evidence_ids=(stored.evidence_id,),
            rationale=rationale,
            actor=actor,
        )

    def propose_from_evidence(
        self,
        procedure: EmployeeSkillProcedure,
        *,
        evidence_ids: tuple[str, ...],
        rationale: str,
        actor: str,
    ) -> EmployeeSkillPatchCandidate:
        procedure = validate_employee_skill_procedure(procedure)
        self._ensure_employee(procedure.employee_id)
        rationale = _text(rationale, "rationale", maximum=500)
        actor = _text(actor, "proposal actor", maximum=120)
        normalized_ids = tuple(sorted(_text(item, "evidence id", maximum=120) for item in evidence_ids))
        if not normalized_ids or len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("Employee Skill Patch requires unique evidence ids")
        evidence = tuple(self.store.get_employee_skill_evidence(item) for item in normalized_ids)
        procedure_hash = content_digest(procedure.content_payload())
        if any(
            (
                item.employee_id,
                item.skill_key,
                item.context_key,
                item.procedure_hash,
            )
            != (
                procedure.employee_id,
                procedure.skill_key,
                procedure.context_key,
                procedure_hash,
            )
            for item in evidence
        ):
            raise ValueError("Employee Skill evidence does not match the procedure")
        user_path = (
            len(evidence) == 1
            and evidence[0].kind == EmployeeSkillEvidenceKind.USER_CORRECTION
            and evidence[0].confirmed_by_user
        )
        job_path = (
            len(evidence) >= 2
            and all(
                item.kind == EmployeeSkillEvidenceKind.VERIFIED_JOB_PROCEDURE
                and item.source.production_eligible
                and item.job_succeeded
                and item.validation_passed
                and item.safety_passed
                for item in evidence
            )
            and len({item.source_ref for item in evidence}) == len(evidence)
        )
        if not user_path and not job_path:
            raise ValueError(
                "Employee Skill Patch requires one confirmed correction or two independent safe jobs"
            )
        current = self.store.current_employee_skill(
            procedure.employee_id,
            procedure.skill_key,
            procedure.context_key,
        )
        before = current.procedure if current is not None and current.active else None
        base_revision = current.revision if current is not None else 0
        immutable = {
            "base_company_revision": self.store.company().revision,
            "base_playbook_revision": self.store.playbook().revision,
            "base_roster_revision": self.store.roster().revision,
            "base_skill_revision": base_revision,
            "procedure": procedure,
            "before_procedure": before,
            "evidence_ids": normalized_ids,
            "rationale": rationale,
            "proposed_by": actor,
        }
        digest = content_digest(immutable)
        now = utc_now().isoformat()
        candidate = EmployeeSkillPatchCandidate(
            patch_id=f"employee-skill-patch-{digest[:24]}",
            status=EmployeeSkillPatchStatus.PROPOSED,
            **immutable,
            content_hash=digest,
            created_at=now,
            updated_at=now,
        )
        return self.store.create_employee_skill_patch(candidate, actor=actor)[0]

    def runtime_snapshots(
        self,
        employee_ids: Sequence[str],
        *,
        context_key: str,
        query: str = "",
        limit_per_employee: int = 3,
    ) -> Mapping[str, tuple[VersionedContent, ...]]:
        candidates = self.store.employee_skill_runtime_snapshots(employee_ids, context_key)
        retriever = BoundedKnowledgeRetriever()
        return {
            employee_id: retriever.select(
                items,
                query=query,
                limit=limit_per_employee,
                max_bytes=12_000,
                # Administrative/observation callers without a task query must
                # retain the previous bounded exact-context snapshot behavior.
                fallback_count=1 if query.strip() else limit_per_employee,
            ).items
            for employee_id, items in candidates.items()
        }

    def preview(self, patch_id: str) -> EmployeeSkillPatchCandidate:
        return self.store.get_employee_skill_patch(patch_id)

    def approve(self, patch_id: str, *, actor: str) -> EmployeeSkillPatchCandidate:
        return self.store.approve_employee_skill_patch(patch_id, actor)

    def apply(self, patch_id: str, *, actor: str) -> EmployeeSkillPatchCandidate:
        return self.store.apply_employee_skill_patch(patch_id, actor)

    def reject(
        self,
        patch_id: str,
        *,
        actor: str,
        reason: str,
    ) -> EmployeeSkillPatchCandidate:
        return self.store.reject_employee_skill_patch(patch_id, actor, reason)

    def rollback(self, patch_id: str, *, actor: str) -> EmployeeSkillPatchCandidate:
        return self.store.rollback_employee_skill_patch(patch_id, actor)

    def observe(
        self,
        patch_id: str,
        episode: OrganizationEpisode,
        runs: Sequence[Mapping[str, object]],
    ) -> EmployeeSkillObservation:
        contract = self.store.get_employee_skill_observation_contract(patch_id)
        existing = next(
            (
                item
                for item in self.store.list_employee_skill_observations(patch_id)
                if item.episode_id == episode.episode_id
            ),
            None,
        )
        cohort_count = sum(
            item.cohort_eligible
            for item in self.store.list_employee_skill_observations(patch_id)
            if existing is None or item.observation_id != existing.observation_id
        )
        observation = employee_skill_observation_from_runtime_ledger(
            episode,
            runs,
            contract=contract,
            existing_cohort_count=cohort_count,
        )
        return self.store.record_employee_skill_observation(observation)[0]

    def assess(self, patch_id: str) -> EmployeeSkillAssessment:
        contract = self.store.get_employee_skill_observation_contract(patch_id)
        observations = self.store.list_employee_skill_observations(patch_id)
        cohort = tuple(item for item in observations if item.cohort_eligible)[
            : contract.maximum_observations
        ]
        exposed_count = sum(item.skill_exposed for item in cohort)
        decision = EmployeeSkillAssessmentDecision.INSUFFICIENT_OBSERVATION
        if any(not item.safety_passed for item in cohort):
            decision = EmployeeSkillAssessmentDecision.ROLLBACK_CANDIDATE
            reasons = ("attributed_failure_or_safety_violation",)
        elif len(cohort) < contract.minimum_observations:
            reasons = ("minimum_observation_count_not_reached",)
        elif all(item.skill_exposed for item in cohort):
            decision = EmployeeSkillAssessmentDecision.KEEP
            reasons = ("exact_skill_version_used_safely",)
        elif len(cohort) >= contract.maximum_observations:
            decision = EmployeeSkillAssessmentDecision.ROLLBACK_CANDIDATE
            reasons = ("skill_not_exposed_within_observation_limit",)
        else:
            reasons = ("skill_exposure_not_yet_proven",)
        immutable = {
            "patch_id": patch_id,
            "decision": decision,
            "reasons": reasons,
            "observation_ids": tuple(item.observation_id for item in cohort),
            "exposed_count": exposed_count,
        }
        digest = content_digest(immutable)
        previous = self.store.list_employee_skill_assessments(patch_id)
        assessment = EmployeeSkillAssessment(
            assessment_id=f"employee-skill-assessment-{digest[:24]}",
            seq=len(previous) + 1,
            **immutable,
            content_hash=digest,
            assessed_at=utc_now().isoformat(),
        )
        return self.store.record_employee_skill_assessment(assessment)[0]
