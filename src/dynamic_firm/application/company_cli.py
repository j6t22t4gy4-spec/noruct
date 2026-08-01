"""Application adapter for Noruct Company CLI commands.

The public parser, configuration loading, exit/error boundary and command name
belong to :mod:`dynamic_firm.cli`.  This adapter receives an already-parsed
command and an explicit local state path, then opens the existing Company and
runtime stores to execute their ordinary lifecycle APIs.  It never becomes a
second Company, Kernel, budget or approval authority.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

from dynamic_firm.company import (
    CompanyLearningService,
    CompanyStateStore,
    EmployeeSkillPatchService,
    EmployeeSkillProcedure,
    EvolutionAutonomyMode,
    HireObservationService,
    HiringRecommendationService,
    MANAGER_CAPABILITY,
    PersistentExecutiveManager,
    RetentionReviewMode,
    RosterPatchService,
    RosterRetentionService,
    WorkflowPatchPromotionService,
    assess_manager_outcomes,
    assess_organization_outcomes,
    decode_active_roster,
    manager_operating_report,
    organization_metric_report,
    verify_live_evidence_pair,
)
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.product.company_commands import (
    propose_roster_patch,
    run_company_curate_daemon,
)
from dynamic_firm.product.company_command_renderer import render_company_command_result
from dynamic_firm.product.cross_plane_attention import (
    MAX_SUPPLEMENTAL_ATTENTION_LIMIT,
    inspect_supplemental_operator_attention,
)
from dynamic_firm.runtime.company_budget import (
    CompanyCostBudgetPolicy,
    SQLiteCompanyBudgetAuthority,
)
from dynamic_firm.runtime.operator_attention import CompanyAttentionInspector
from dynamic_firm.runtime.store import RunStore


COMPANY_COMMAND_OK = 0
COMPANY_COMMAND_JOB_FAILED = 4


def run_company_command(
    args: argparse.Namespace,
    *,
    state_path: Path,
    output: TextIO,
) -> int:
    """Dispatch one parsed Company command without owning CLI ingress or state authority."""

    path = state_path
    if args.company_command == "curate-daemon":
        return run_company_curate_daemon(args, state_path=path, output=output)
    mutating = {
        "approve",
        "apply",
        "rollback",
        "reject",
        "evidence-import",
        "roster-approve",
        "roster-apply",
        "roster-reject",
        "review-policy-set",
        "autonomy-set",
        "budget-policy-set",
        "budget-resolve",
        "skill-propose",
        "skill-approve",
        "skill-apply",
        "skill-reject",
        "skill-rollback",
        "workflow-promote",
    }
    if args.company_command in mutating and not args.confirm:
        raise ValueError(
            f"Company {args.company_command} requires --confirm; no state was changed."
        )
    with CompanyStateStore(path) as store:
        learning = CompanyLearningService(store)
        roster_patches = RosterPatchService(store)
        hiring = HiringRecommendationService(store)
        hire_observation = HireObservationService(store)
        retention = RosterRetentionService(store)
        skill_learning = EmployeeSkillPatchService(store)
        workflow_promotion = WorkflowPatchPromotionService(store)
        try:
            if args.company_command == "status":
                payload: object = {
                    "schema_version": store.schema_version(),
                    "summary": store.summary(),
                    "company": store.company(),
                    "roster": store.roster(),
                    "playbook": store.playbook(),
                }
            elif args.company_command == "episodes":
                if args.limit < 1 or args.limit > 1_000:
                    raise ValueError("Company episode limit must be between 1 and 1000")
                payload = store.list_episodes(args.limit)
            elif args.company_command == "patches":
                payload = store.list_patches()
            elif args.company_command == "roster-patches":
                payload = roster_patches.list()
            elif args.company_command == "staffing-demands":
                payload = store.list_staffing_demands()
            elif args.company_command == "hire-contracts":
                payload = store.list_hire_observation_contracts()
            elif args.company_command == "retention-reviews":
                payload = store.list_retention_reviews()
            elif args.company_command == "manager-status":
                snapshot = decode_active_roster(store.roster())
                manager = PersistentExecutiveManager.optional_from_roster(
                    snapshot.employees,
                    roster_revision=snapshot.revision,
                )
                payload = {
                    "roster_revision": snapshot.revision,
                    "manager_capable": manager is not None,
                    "manager": (
                        {
                            "employee_id": manager.identity.employee_id,
                            "role": manager.identity.role,
                            "model_profile": manager.identity.model_profile,
                            "memory_namespace": manager.identity.memory_namespace,
                        }
                        if manager is not None
                        else None
                    ),
                    "migration": (
                        "not_required"
                        if manager is not None
                        else "propose_with_company_manager_migrate_then_explicitly_roster_approve_and_roster_apply"
                    ),
                    "authority": "kernel_retained",
                }
            elif args.company_command == "manager-outcomes":
                payload = assess_manager_outcomes(
                    store.list_episodes(),
                    manager_employee_id=args.manager_id,
                    context_fingerprint=args.context_fingerprint,
                )
            elif args.company_command == "manager-report":
                active_roster = store.roster()
                snapshot = (
                    decode_active_roster(active_roster)
                    if active_roster.employees
                    else None
                )
                manager = (
                    PersistentExecutiveManager.optional_from_roster(
                        snapshot.employees,
                        roster_revision=snapshot.revision,
                    )
                    if snapshot is not None
                    else None
                )
                payload = manager_operating_report(
                    manager,
                    store.list_episodes(),
                    skill_versions=(
                        store.list_employee_skills(
                            employee_id=manager.identity.employee_id,
                            active_only=True,
                        )
                        if manager is not None
                        else ()
                    ),
                )
            elif args.company_command == "organization-metrics":
                payload = organization_metric_report(store.list_episodes())
            elif args.company_command == "organization-outcomes":
                episodes = store.list_episodes()
                contexts = (
                    (args.context_fingerprint,)
                    if args.context_fingerprint
                    else tuple(
                        sorted(
                            {
                                item.context_fingerprint
                                for item in episodes
                                if item.context_fingerprint
                            }
                        )
                    )
                )
                payload = {
                    "schema_version": "noruct.organization-outcome-assessments.v1",
                    "assessments": tuple(
                        assess_organization_outcomes(
                            episodes,
                            context_fingerprint=context,
                        )
                        for context in contexts
                    ),
                    "automatic_application": "next_job_only_evidence_gated",
                    "state_changed": False,
                }
            elif args.company_command == "manager-migrate":
                snapshot = decode_active_roster(store.roster())
                existing = PersistentExecutiveManager.optional_from_roster(
                    snapshot.employees,
                    roster_revision=snapshot.revision,
                )
                if existing is not None:
                    payload = {
                        "manager_capable": True,
                        "manager_employee_id": existing.identity.employee_id,
                        "roster_revision": snapshot.revision,
                        "changed": False,
                        "reason": "persistent_manager_already_present",
                    }
                else:
                    candidate = roster_patches.propose_add_employee(
                        EmployeeRecord(
                            employee_id=args.employee_id,
                            role=args.role,
                            capabilities=(MANAGER_CAPABILITY,),
                            model_profile=args.model_profile,
                        ),
                        rationale=(
                            "Explicit operator migration to the M2 persistent Manager "
                            "baseline; this proposal grants no authority and must pass the "
                            "normal Roster approval/apply lifecycle."
                        ),
                        actor="user:cli:manager-migrate",
                    )
                    payload = {
                        "manager_capable": False,
                        "roster_revision": snapshot.revision,
                        "changed": True,
                        "proposal": candidate,
                        "next_steps": (
                            f"noruct company roster-approve {candidate.patch_id} --confirm",
                            f"noruct company roster-apply {candidate.patch_id} --confirm",
                        ),
                        "authority": "kernel_retained",
                    }
            elif args.company_command == "manager-revise":
                snapshot = decode_active_roster(store.roster())
                manager = PersistentExecutiveManager.optional_from_roster(
                    snapshot.employees,
                    roster_revision=snapshot.revision,
                )
                if manager is None:
                    raise ValueError("Manager revision requires a persistent Manager; run manager-migrate first")
                current = next(
                    employee
                    for employee in snapshot.employees
                    if employee.employee_id == manager.identity.employee_id
                )
                candidate = roster_patches.propose_update_employee(
                    EmployeeRecord(
                        employee_id=current.employee_id,
                        role=args.role or current.role,
                        capabilities=current.capabilities,
                        active=current.active,
                        temporary=False,
                        model_profile=args.model_profile or current.model_profile,
                    ),
                    rationale=args.rationale,
                    actor="user:cli:manager-revise",
                )
                payload = {
                    "manager_employee_id": current.employee_id,
                    "proposal": candidate,
                    "active_roster_revision": snapshot.revision,
                    "active_jobs_changed": False,
                    "next_steps": (
                        f"noruct company roster-approve {candidate.patch_id} --confirm",
                        f"noruct company roster-apply {candidate.patch_id} --confirm",
                    ),
                }
            elif args.company_command == "manager-rollback":
                snapshot = decode_active_roster(store.roster())
                manager = PersistentExecutiveManager.optional_from_roster(
                    snapshot.employees,
                    roster_revision=snapshot.revision,
                )
                if manager is None:
                    raise ValueError("Manager rollback requires a persistent Manager")
                prior = store.roster_at_revision(args.roster_revision)
                prior_employee = next(
                    (
                        employee
                        for employee in prior.employees
                        if employee.get("employee_id") == manager.identity.employee_id
                        and MANAGER_CAPABILITY in employee.get("capabilities", ())
                    ),
                    None,
                )
                if prior_employee is None:
                    raise ValueError("Requested ROSTER revision has no matching Manager identity")
                candidate = roster_patches.propose_update_employee(
                    EmployeeRecord(
                        employee_id=str(prior_employee["employee_id"]),
                        role=str(prior_employee["role"]),
                        capabilities=tuple(str(item) for item in prior_employee["capabilities"]),
                        active=bool(prior_employee.get("active", True)),
                        temporary=False,
                        model_profile=str(prior_employee["model_profile"]),
                    ),
                    rationale=args.rationale,
                    actor="user:cli:manager-rollback",
                )
                payload = {
                    "manager_employee_id": manager.identity.employee_id,
                    "restore_from_roster_revision": prior.revision,
                    "proposal": candidate,
                    "active_roster_revision": snapshot.revision,
                    "active_jobs_changed": False,
                    "next_steps": (
                        f"noruct company roster-approve {candidate.patch_id} --confirm",
                        f"noruct company roster-apply {candidate.patch_id} --confirm",
                    ),
                }
            elif args.company_command == "employee-skills":
                payload = store.list_employee_skills(
                    employee_id=args.employee_id,
                    context_key=args.context_key,
                )
            elif args.company_command == "skill-patches":
                payload = store.list_employee_skill_patches()
            elif args.company_command == "skill-propose":
                procedure = EmployeeSkillProcedure(
                    employee_id=args.employee_id,
                    skill_key=args.skill_key,
                    context_key=args.context_key,
                    purpose=args.purpose,
                    steps=tuple(args.step),
                    verification_steps=tuple(args.verify),
                    prohibitions=tuple(args.prohibition),
                )
                patch = skill_learning.propose_user_correction(
                    procedure,
                    correction_id=args.correction_id,
                    rationale=args.rationale,
                    actor="user:cli",
                )
                payload = {
                    "patch": patch,
                    "evidence": tuple(
                        store.get_employee_skill_evidence(item)
                        for item in patch.evidence_ids
                    ),
                    "events": store.list_employee_skill_patch_events(patch.patch_id),
                    "review_mode": "approval",
                    "active_skill_changed": False,
                }
            elif args.company_command == "skill-preview":
                patch = skill_learning.preview(args.patch_id)
                try:
                    contract = store.get_employee_skill_observation_contract(args.patch_id)
                except KeyError:
                    contract = None
                observations = store.list_employee_skill_observations(args.patch_id)
                assessments = store.list_employee_skill_assessments(args.patch_id)
                payload = {
                    "patch": patch,
                    "evidence": tuple(
                        store.get_employee_skill_evidence(item)
                        for item in patch.evidence_ids
                    ),
                    "events": store.list_employee_skill_patch_events(args.patch_id),
                    "observation_contract": contract,
                    "observations": observations,
                    "latest_assessment": assessments[-1] if assessments else None,
                    "review_mode": "approval",
                }
            elif args.company_command == "skill-assess":
                patch = skill_learning.preview(args.patch_id)
                before = store.current_employee_skill(
                    patch.procedure.employee_id,
                    patch.procedure.skill_key,
                    patch.procedure.context_key,
                )
                assessment = skill_learning.assess(args.patch_id)
                after = store.current_employee_skill(
                    patch.procedure.employee_id,
                    patch.procedure.skill_key,
                    patch.procedure.context_key,
                )
                payload = {
                    "assessment": assessment,
                    "skill_revision_before": None if before is None else before.revision,
                    "skill_revision_after": None if after is None else after.revision,
                    "automatic_rollback": False,
                }
            elif args.company_command == "review-policy":
                payload = {
                    "company_revision": store.company().revision,
                    "mode": store.retention_review_mode(),
                    "scope": "evidence-backed-reversible-retention-only",
                    "policy_events": store.list_company_policy_events(),
                    "state_changed": False,
                }
            elif args.company_command == "review-policy-set":
                before_revision = store.company().revision
                company, changed = store.set_retention_review_mode(
                    RetentionReviewMode(args.mode),
                    actor="user:cli",
                )
                payload = {
                    "company": company,
                    "mode": store.retention_review_mode(),
                    "company_revision_before": before_revision,
                    "company_revision_after": company.revision,
                    "changed": changed,
                    "scope": "evidence-backed-reversible-retention-only",
                    "hard_invariants_bypassed": False,
                }
            elif args.company_command == "autonomy":
                mode = store.evolution_autonomy_mode()
                payload = {
                    "company_revision": store.company().revision,
                    "mode": mode,
                    "label": {
                        EvolutionAutonomyMode.NEVER: "Never adopt improvements automatically",
                        EvolutionAutonomyMode.PROPOSE: "Propose verified improvements",
                        EvolutionAutonomyMode.ALWAYS_APPROVE: "Automatically adopt verified improvements",
                    }[mode],
                    "scope": {
                        "network_artifacts": "explicit-exact-version-activation-only",
                        "local_derived_artifacts": "off" if mode == EvolutionAutonomyMode.NEVER else ("proposal" if mode == EvolutionAutonomyMode.PROPOSE else "compatible-stable-next-job"),
                        "workflow_patches": "off" if mode == EvolutionAutonomyMode.NEVER else ("proposal" if mode == EvolutionAutonomyMode.PROPOSE else "eligible-auto-apply"),
                        "roster_hiring": "off" if mode == EvolutionAutonomyMode.NEVER else ("proposal" if mode == EvolutionAutonomyMode.PROPOSE else "evidence-gated-auto-hire"),
                        "employee_skill": "off" if mode == EvolutionAutonomyMode.NEVER else ("proposal" if mode == EvolutionAutonomyMode.PROPOSE else "verified-next-job"),
                    },
                    "always_protected": (
                        "unsigned_or_incompatible_artifacts",
                        "missing_credentials",
                        "authority_escalation",
                        "cost_limit_exceeded",
                        "running_job_version_change",
                    ),
                    "policy_events": store.list_company_policy_events(),
                    "state_changed": False,
                }
            elif args.company_command == "autonomy-set":
                before_revision = store.company().revision
                company, changed = store.set_evolution_autonomy_mode(
                    EvolutionAutonomyMode(args.mode), actor="user:cli"
                )
                payload = {
                    "company": company,
                    "mode": store.evolution_autonomy_mode(),
                    "company_revision_before": before_revision,
                    "company_revision_after": company.revision,
                    "changed": changed,
                    "hard_invariants_bypassed": False,
                }
            elif args.company_command == "budget-status":
                policy = CompanyCostBudgetPolicy.from_mapping(
                    store.company_cost_budget_policy()
                )
                runtime_store = RunStore(path)
                try:
                    payload = SQLiteCompanyBudgetAuthority(runtime_store, policy).status()
                finally:
                    runtime_store.close()
            elif args.company_command == "attention":
                policy = CompanyCostBudgetPolicy.from_mapping(
                    store.company_cost_budget_policy()
                )
                runtime_store = RunStore(path)
                try:
                    company_attention = CompanyAttentionInspector(runtime_store, policy).inspect(
                        job_limit=args.limit
                    )
                finally:
                    runtime_store.close()
                payload = {
                    **asdict(company_attention),
                    "supplemental": asdict(
                        inspect_supplemental_operator_attention(
                            path,
                            limit=min(args.limit, MAX_SUPPLEMENTAL_ATTENTION_LIMIT),
                        )
                    ),
                }
            elif args.company_command == "budget-policy-set":
                before_revision = store.company().revision
                company, changed = store.set_company_cost_budget_policy(
                    {
                        "max_total_cost_usd": args.max_total_cost_usd,
                        "window_kind": args.window_kind,
                    },
                    actor="user:cli",
                )
                policy = CompanyCostBudgetPolicy.from_mapping(
                    store.company_cost_budget_policy()
                )
                runtime_store = RunStore(path)
                try:
                    status = SQLiteCompanyBudgetAuthority(runtime_store, policy).status()
                finally:
                    runtime_store.close()
                payload = {
                    "company": company,
                    "company_revision_before": before_revision,
                    "company_revision_after": company.revision,
                    "changed": changed,
                    "budget": status,
                    "automatic_resume": False,
                }
            elif args.company_command == "budget-resolve":
                before_revision = store.company().revision
                company, changed = store.set_company_cost_budget_policy(
                    {
                        "max_total_cost_usd": args.max_total_cost_usd,
                        "window_kind": args.window_kind,
                    },
                    actor="user:cli",
                )
                policy = CompanyCostBudgetPolicy.from_mapping(
                    store.company_cost_budget_policy()
                )
                runtime_store = RunStore(path)
                try:
                    authority = SQLiteCompanyBudgetAuthority(runtime_store, policy)
                    incident = authority.resolve_incident(
                        args.incident_id,
                        actor="user:cli",
                    )
                    status = authority.status()
                finally:
                    runtime_store.close()
                payload = {
                    "company": company,
                    "company_revision_before": before_revision,
                    "company_revision_after": company.revision,
                    "policy_changed": changed,
                    "incident": incident,
                    "budget": status,
                    "automatic_resume": False,
                }
            elif args.company_command == "evidence-pairs":
                payload = store.list_live_evidence_pairs()
            elif args.company_command in {"evidence-preview", "evidence-import"}:
                pair = verify_live_evidence_pair(args.baseline, args.dynamic)
                conflicts = store.live_evidence_conflicts(pair)
                if args.company_command == "evidence-preview":
                    payload = {
                        "valid": True,
                        "importable": not conflicts,
                        "duplicate_conflicts": conflicts,
                        "pair": pair,
                        "state_changed": False,
                    }
                else:
                    if conflicts:
                        raise ValueError(
                            "Live evidence pair is duplicate: " + ", ".join(conflicts)
                        )
                    episode = store.import_live_evidence_pair(pair)
                    payload = {
                        "imported": True,
                        "pair": pair,
                        "episode": episode,
                        "automatic_curation": False,
                    }
            elif args.company_command == "workflow-promote-preview":
                payload = workflow_promotion.preview(args.pair_directory)
            elif args.company_command == "workflow-promote":
                payload = workflow_promotion.promote(
                    args.pair_directory,
                    actor="user:cli",
                )
            elif args.company_command == "curate":
                payload = learning.curate()
            elif args.company_command == "roster-recommend":
                recommendation = hiring.curate()
                mode = store.evolution_autonomy_mode()
                applied_hires: list[str] = []
                if mode == EvolutionAutonomyMode.ALWAYS_APPROVE:
                    for candidate in recommendation.candidates:
                        evidence = store.roster_patch_evidence(candidate.patch_id)
                        if not evidence or not all(item.production_eligible for item in evidence):
                            continue
                        approved = roster_patches.approve(
                            candidate.patch_id,
                            actor="user-policy:evolution-always-approve",
                        )
                        roster_patches.apply(
                            approved.patch_id,
                            actor="user-policy:evolution-always-approve",
                        )
                        applied_hires.append(candidate.patch_id)
                payload = {
                    "decision": recommendation.decision,
                    "candidates": recommendation.candidates,
                    "considered_evidence_count": recommendation.considered_evidence_count,
                    "qualified_evidence_count": recommendation.qualified_evidence_count,
                    "reasons": recommendation.reasons,
                    "evidence_by_patch": {
                        candidate.patch_id: store.roster_patch_evidence(
                            candidate.patch_id
                        )
                        for candidate in recommendation.candidates
                    },
                    "autonomy_mode": mode,
                    "automatic_approve": mode == EvolutionAutonomyMode.ALWAYS_APPROVE,
                    "automatic_apply": bool(applied_hires),
                    "automatically_applied_hire_patch_ids": tuple(applied_hires),
                    "active_roster_revision": store.roster().revision,
                }
            elif args.company_command == "roster-propose":
                patch = propose_roster_patch(args, roster_patches)
                evidence = store.roster_patch_evidence(patch.patch_id)
                payload = {
                    "patch": patch,
                    "evidence": evidence,
                    "evidence_eligible_for_apply": all(
                        item.production_eligible for item in evidence
                    ),
                    "events": store.list_roster_patch_events(patch.patch_id),
                    "active_roster_revision": store.roster().revision,
                    "state_changed": False,
                }
            elif args.company_command == "roster-preview":
                patch = roster_patches.preview(args.patch_id)
                evidence = store.roster_patch_evidence(args.patch_id)
                assessments = store.roster_patch_assessments(args.patch_id)
                payload = {
                    "patch": patch,
                    "evidence": evidence,
                    "assessments": assessments,
                    "retention_reviews": store.list_retention_reviews(args.patch_id),
                    "evidence_eligible_for_apply": all(
                        item.production_eligible for item in evidence
                    ),
                    "events": store.list_roster_patch_events(args.patch_id),
                    "active_roster_revision": store.roster().revision,
                    "state_changed": False,
                }
            elif args.company_command == "hire-preview":
                observations = store.list_hire_observations(args.patch_id)
                assessments = store.list_hire_assessments(args.patch_id)
                payload = {
                    "patch": roster_patches.preview(args.patch_id),
                    "contract": store.get_hire_observation_contract(args.patch_id),
                    "observations": observations,
                    "attribution": {
                        "total": len(observations),
                        "attributable": sum(
                            item.attribution_eligible for item in observations
                        ),
                        "cohort_eligible": sum(
                            item.cohort_eligible for item in observations
                        ),
                        "persistent_assignment": sum(
                            item.persistent_employee_assigned for item in observations
                            if item.cohort_eligible
                        ),
                        "temporary_fallback": sum(
                            item.temporary_fallback_used for item in observations
                            if item.cohort_eligible
                        ),
                    },
                    "latest_assessment": assessments[-1] if assessments else None,
                    "active_roster_revision": store.roster().revision,
                    "state_changed": False,
                }
            elif args.company_command == "hire-assess":
                before_revision = store.roster().revision
                assessment = hire_observation.assess(args.patch_id)
                payload = {
                    "assessment": assessment,
                    "roster_revision_before": before_revision,
                    "roster_revision_after": store.roster().revision,
                    "automatic_set_active": False,
                    "automatic_roster_patch": False,
                }
            elif args.company_command == "roster-retention-recommend":
                result = retention.recommend(args.hire_patch_id)
                payload = {
                    "result": result,
                    "contract": store.get_hire_observation_contract(
                        args.hire_patch_id
                    ),
                    "assessment": store.latest_hire_assessment(
                        args.hire_patch_id
                    ),
                    "events": store.list_roster_patch_events(result.patch.patch_id),
                    "hard_invariants_bypassed": False,
                    "background_execution": False,
                }
            elif args.company_command == "preview":
                patch = learning.preview(args.patch_id)
                try:
                    observation_contract = store.get_observation_contract(args.patch_id)
                    observations = store.list_observations(args.patch_id)
                    assessments = store.list_assessments(args.patch_id)
                except KeyError:
                    observation_contract = None
                    observations = ()
                    assessments = ()
                payload = {
                    "patch": patch,
                    "evidence": tuple(
                        store.get_episode(item) for item in patch.evidence_episode_ids
                    ),
                    "events": store.list_patch_events(args.patch_id),
                    "observation_contract": observation_contract,
                    "observations": observations,
                    "latest_assessment": assessments[-1] if assessments else None,
                }
            elif args.company_command == "observe":
                observations = store.list_observations(args.patch_id)
                assessments = store.list_assessments(args.patch_id)
                payload = {
                    "patch": learning.preview(args.patch_id),
                    "contract": store.get_observation_contract(args.patch_id),
                    "observations": observations,
                    "attribution": {
                        "total": len(observations),
                        "prior_exposed": sum(item.prior_exposed for item in observations),
                        "proposal_aligned": sum(
                            item.proposal_aligned for item in observations
                        ),
                        "attributable": sum(
                            item.attribution_eligible for item in observations
                        ),
                        "cohort_eligible": sum(
                            item.cohort_eligible for item in observations
                        ),
                    },
                    "latest_assessment": assessments[-1] if assessments else None,
                }
            elif args.company_command == "assess":
                before_revision = store.playbook().revision
                assessment = learning.assess(args.patch_id)
                payload = {
                    "assessment": assessment,
                    "playbook_revision_before": before_revision,
                    "playbook_revision_after": store.playbook().revision,
                    "automatic_rollback": False,
                }
            elif args.company_command == "replay":
                matches = learning.replay(args.patch_id)
                payload = {"patch_id": args.patch_id, "replay_matches": matches}
            elif args.company_command == "approve":
                payload = learning.approve(args.patch_id, actor="user:cli")
            elif args.company_command == "apply":
                payload = learning.apply(args.patch_id, actor="user:cli")
            elif args.company_command == "rollback":
                payload = learning.rollback(args.patch_id, actor="user:cli")
            elif args.company_command == "reject":
                payload = learning.reject(
                    args.patch_id,
                    actor="user:cli",
                    reason=args.reason,
                )
            elif args.company_command == "roster-approve":
                payload = roster_patches.approve(args.patch_id, actor="user:cli")
            elif args.company_command == "roster-apply":
                payload = roster_patches.apply(args.patch_id, actor="user:cli")
            elif args.company_command == "roster-reject":
                payload = roster_patches.reject(
                    args.patch_id,
                    actor="user:cli",
                    reason=args.reason,
                )
            elif args.company_command == "skill-approve":
                payload = skill_learning.approve(args.patch_id, actor="user:cli")
            elif args.company_command == "skill-apply":
                payload = skill_learning.apply(args.patch_id, actor="user:cli")
            elif args.company_command == "skill-reject":
                payload = skill_learning.reject(
                    args.patch_id,
                    actor="user:cli",
                    reason=args.reason,
                )
            elif args.company_command == "skill-rollback":
                payload = skill_learning.rollback(args.patch_id, actor="user:cli")
            else:
                raise ValueError(f"Unknown company command: {args.company_command}")
        except KeyError as exc:
            raise ValueError(str(exc).strip("'")) from None
        active_playbook_revision = store.playbook().revision
        active_roster_revision = store.roster().revision

    return render_company_command_result(
        args,
        payload=payload,
        active_playbook_revision=active_playbook_revision,
        active_roster_revision=active_roster_revision,
        output=output,
    )
