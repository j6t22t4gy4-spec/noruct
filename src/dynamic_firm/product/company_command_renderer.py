"""Terminal rendering for already-dispatched Company command results.

This module accepts the sanitized result from the Company application adapter.
It owns no Company Store, Job, provider, Kernel, budget, approval, or patch
lifecycle and never performs a state transition while formatting output.
"""

from __future__ import annotations

import argparse
import json
from typing import TextIO

from dynamic_firm.product.company_commands import (
    render_company_observability,
    render_roster_patch_preview,
)
from dynamic_firm.runtime.models import to_primitive


COMPANY_COMMAND_OK = 0
COMPANY_COMMAND_JOB_FAILED = 4


def render_company_command_result(
    args: argparse.Namespace,
    *,
    payload: object,
    active_playbook_revision: int,
    active_roster_revision: int,
    output: TextIO,
) -> int:
    """Render a completed Company command result without modifying authority."""

    primitive = to_primitive(payload)
    if args.json:
        print(json.dumps(primitive, ensure_ascii=False, sort_keys=True), file=output)
    elif render_company_observability(args.company_command, primitive, output):
        pass
    elif args.company_command == "curate":
        print(
            f"{primitive['decision']} · considered={primitive['considered_episode_count']} · "
            f"qualified={primitive['qualified_episode_count']} · "
            f"candidate(s)={len(primitive['candidates'])}",
            file=output,
        )
        for candidate in primitive["candidates"]:
            mode = "applicable after approval" if candidate["eligible_for_apply"] else "preview only"
            print(f"- {candidate['patch_id']} · {candidate['pattern']['task_family']} · {mode}", file=output)
        for reason in primitive["reasons"]:
            print(f"- {reason}", file=output)
    elif args.company_command == "roster-recommend":
        print(
            f"{primitive['decision']} · "
            f"considered={primitive['considered_evidence_count']} · "
            f"qualified={primitive['qualified_evidence_count']} · "
            f"candidate(s)={len(primitive['candidates'])}",
            file=output,
        )
        for candidate in primitive["candidates"]:
            evidence = primitive["evidence_by_patch"][candidate["patch_id"]]
            eligible = all(
                item["source"] in {"REAL_JOB", "LIVE_EVALUATION"}
                for item in evidence
            )
            print(
                f"- {candidate['patch_id']} · {candidate['after_employee']['role']} · "
                f"evidence={len(evidence)} · "
                f"{'applicable after approval' if eligible else 'preview only'}",
                file=output,
            )
        for reason in primitive["reasons"]:
            print(f"- {reason}", file=output)
        if primitive["automatic_apply"]:
            print(
                "Automatically hired: "
                + ", ".join(primitive["automatically_applied_hire_patch_ids"]),
                file=output,
            )
        else:
            print(
                f"Automatic hire: {primitive['autonomy_mode']} · no eligible new hire applied",
                file=output,
            )
    elif args.company_command == "staffing-demands":
        if not primitive:
            print("No staffing demand evidence.", file=output)
        for evidence in primitive:
            print(
                f"{evidence['evidence_id']} · {evidence['capability']} · "
                f"job={evidence['job_id']} · source={evidence['source']} · "
                f"safe={str(bool(evidence['job_succeeded'] and not evidence['safety_violations'])).lower()}",
                file=output,
            )
    elif args.company_command == "replay":
        print(
            f"{primitive['patch_id']} · replay "
            f"{'matched' if primitive['replay_matches'] else 'mismatch'}",
            file=output,
        )
    elif args.company_command == "observe":
        attribution = primitive["attribution"]
        latest = primitive["latest_assessment"]
        print(
            f"{primitive['patch']['patch_id']} · observations={attribution['total']} · "
            f"exposed={attribution['prior_exposed']} · aligned={attribution['proposal_aligned']} · "
            f"cohort={attribution['cohort_eligible']}",
            file=output,
        )
        print(
            "Assessment: "
            + (latest["decision"] if latest else "INSUFFICIENT_OBSERVATION (not assessed)"),
            file=output,
        )
    elif args.company_command == "assess":
        assessment = primitive["assessment"]
        print(
            f"{assessment['patch_id']} · {assessment['decision']} · "
            f"cohort={len(assessment['cohort_observation_ids'])}",
            file=output,
        )
        print("Automatic rollback: disabled", file=output)
    elif args.company_command == "hire-preview":
        attribution = primitive["attribution"]
        contract = primitive["contract"]
        latest = primitive["latest_assessment"]
        print(
            f"{contract['patch_id']} · {contract['employee_id']} · "
            f"{contract['capability']} · ROSTER r{contract['applied_roster_revision']}",
            file=output,
        )
        print(
            f"Observations: {attribution['cohort_eligible']}/"
            f"{contract['maximum_observations']} cohort · "
            f"persistent={attribution['persistent_assignment']} · "
            f"temporary fallback={attribution['temporary_fallback']}",
            file=output,
        )
        print(
            "Assessment: "
            + (latest["decision"] if latest else "INSUFFICIENT_OBSERVATION (not assessed)"),
            file=output,
        )
        print("Automatic employee state change: disabled", file=output)
    elif args.company_command == "hire-assess":
        assessment = primitive["assessment"]
        print(
            f"{assessment['patch_id']} · {assessment['decision']} · "
            f"cohort={len(assessment['cohort_observation_ids'])} · "
            f"persistent={assessment['persistent_assignment_count']} · "
            f"fallback={assessment['temporary_fallback_count']}",
            file=output,
        )
        print("Automatic employee state change: disabled", file=output)
    elif args.company_command == "review-policy":
        print(
            f"Retention review mode: {primitive['mode']} · "
            f"COMPANY r{primitive['company_revision']}",
            file=output,
        )
        print("Scope: reversible evidence-backed dormancy only", file=output)
        print("Hash, stale, exact before/after and ROSTER decoder: always enforced", file=output)
    elif args.company_command == "review-policy-set":
        print(
            f"Retention review mode: {primitive['mode']} · COMPANY r"
            f"{primitive['company_revision_before']} → r"
            f"{primitive['company_revision_after']}",
            file=output,
        )
        print("Scope: reversible evidence-backed dormancy only", file=output)
        print("Hard invariants bypassed: no", file=output)
    elif args.company_command == "autonomy":
        print(
            f"Evolution autonomy: {primitive['mode']} · "
            f"COMPANY r{primitive['company_revision']}",
            file=output,
        )
        print(primitive["label"], file=output)
        scope = primitive["scope"]
        print(
            "Scope: "
            f"network={scope['network_artifacts']} · "
            f"local-derived={scope['local_derived_artifacts']} · "
            f"workflow={scope['workflow_patches']} · "
            f"hiring={scope['roster_hiring']} · skills={scope['employee_skill']}",
            file=output,
        )
        print("Always protected: " + ", ".join(primitive["always_protected"]), file=output)
    elif args.company_command == "autonomy-set":
        print(
            f"Evolution autonomy: {primitive['mode']} · COMPANY r"
            f"{primitive['company_revision_before']} → r"
            f"{primitive['company_revision_after']}",
            file=output,
        )
        print("Qualifying changes use this mode from the next evolution cycle; hard invariants bypassed: no", file=output)
    elif args.company_command == "budget-status":
        policy = primitive["policy"]
        if not primitive["enabled"]:
            print(
                "Company cost budget: disabled · "
                f"paused={str(primitive['paused']).lower()}",
                file=output,
            )
        else:
            print(
                f"Company cost budget: ${policy['max_total_cost_usd']:.6f} · "
                f"{policy['window_kind']} · paused={str(primitive['paused']).lower()}",
                file=output,
            )
            print(
                f"Observed ${primitive['observed_cost_usd']:.6f} · "
                f"reserved ${primitive['reserved_cost_usd']:.6f} · "
                f"remaining ${primitive['remaining_cost_usd']:.6f}",
                file=output,
            )
        if primitive["incident"] is not None:
            print(
                f"Incident: {primitive['incident']['incident_id']} · "
                f"{primitive['incident']['window_kind']} · explicit budget-resolve required",
                file=output,
            )
    elif args.company_command == "attention":
        supplemental = primitive.get("supplemental", {})
        supplemental_items = supplemental.get("items", []) if isinstance(supplemental, dict) else []
        total_items = len(primitive["items"]) + len(supplemental_items)
        print(
            "Company operator attention · "
            f"items={total_items} · jobs={primitive['scanned_job_count']}/"
            f"{primitive['job_scan_limit']}",
            file=output,
        )
        if primitive["jobs_truncated"]:
            print("Recent ACTIVE JOB scan is bounded; older jobs were not inspected.", file=output)
        if not primitive["items"] and not supplemental_items:
            print("No current local operator attention items.", file=output)
        for item in primitive["items"]:
            identity = item["subject_id"]
            context = ""
            if item["job_id"]:
                context += f" · job={item['job_id']}"
            if item["run_id"]:
                context += f" · run={item['run_id']}"
            print(f"[{item['kind']}] {identity} · {item['state']}{context}", file=output)
            print(f"  {item['recommended_action']}", file=output)
        for item in supplemental_items:
            print(f"[{item['kind']}] {item['subject_id']} · {item['state']}", file=output)
            print(f"  {item['recommended_action']}", file=output)
        if isinstance(supplemental, dict) and supplemental.get("truncated"):
            print("Knowledge/Artifact review scan is bounded; older items were not rendered.", file=output)
        if primitive["suppressed_pending_approval_count"]:
            print(
                "Pending approvals already owned by interrupted/invalid jobs: "
                f"{primitive['suppressed_pending_approval_count']} (shown through job recovery only).",
                file=output,
            )
        print("Read-only projection · automatic resolution: disabled", file=output)
    elif args.company_command == "budget-policy-set":
        budget = primitive["budget"]
        policy = budget["policy"]
        print(
            f"Company cost budget: ${policy['max_total_cost_usd']:.6f} · "
            f"{policy['window_kind']} · COMPANY r{primitive['company_revision_before']} → "
            f"r{primitive['company_revision_after']}",
            file=output,
        )
        print("Automatic resume: disabled", file=output)
    elif args.company_command == "budget-resolve":
        print(
            f"Company budget incident resolved: {primitive['incident']['incident_id']} · "
            f"COMPANY r{primitive['company_revision_before']} → "
            f"r{primitive['company_revision_after']}",
            file=output,
        )
        print("Automatic resume: disabled", file=output)
    elif args.company_command == "roster-retention-recommend":
        result = primitive["result"]
        review = result["review"]
        patch = result["patch"]
        print(
            f"{patch['patch_id']} · {patch['employee_id']} · "
            f"{review['mode']} → {review['decision']}",
            file=output,
        )
        print(
            f"ROSTER r{result['roster_revision_before']} → "
            f"r{result['roster_revision_after']} · "
            f"status={patch['status']} · applied={str(result['applied']).lower()}",
            file=output,
        )
        if review["decision"] in {
            "PENDING_USER_APPROVAL",
            "REQUIRES_USER_APPROVAL",
        }:
            print(
                "Next: noruct company roster-preview "
                f"{patch['patch_id']} · then roster-approve/apply --confirm",
                file=output,
            )
        print("Hard invariants bypassed: no", file=output)
    elif args.company_command == "hire-contracts":
        if not primitive:
            print("No hire observation contracts yet.", file=output)
        for contract in primitive:
            print(
                f"{contract['patch_id']} · {contract['employee_id']} · "
                f"{contract['capability']} · ROSTER r{contract['applied_roster_revision']}",
                file=output,
            )
    elif args.company_command == "retention-reviews":
        if not primitive:
            print("No retention reviews yet.", file=output)
        for review in primitive:
            print(
                f"{review['review_id']} · {review['mode']} · "
                f"{review['decision']} · {review['roster_patch_id']}",
                file=output,
            )
    elif args.company_command == "employee-skills":
        if not primitive:
            print("No current employee skills yet.", file=output)
        for skill in primitive:
            print(
                f"{skill['employee_id']} · {skill['skill_key']} · "
                f"context={skill['context_key']} · r{skill['revision']} · "
                f"{'active' if skill['active'] else 'inactive'}",
                file=output,
            )
    elif args.company_command == "skill-patches":
        if not primitive:
            print("No Employee Skill Patches yet.", file=output)
        for patch in primitive:
            procedure = patch["procedure"]
            print(
                f"{patch['patch_id']} · {patch['status']} · "
                f"{procedure['employee_id']}/{procedure['skill_key']} · "
                f"context={procedure['context_key']}",
                file=output,
            )
    elif args.company_command in {"skill-propose", "skill-preview"}:
        patch = primitive["patch"]
        procedure = patch["procedure"]
        print(
            f"{patch['patch_id']} · {patch['status']} · approval only",
            file=output,
        )
        print(
            f"Employee: {procedure['employee_id']} · skill={procedure['skill_key']} · "
            f"context={procedure['context_key']} · base r{patch['base_skill_revision']}",
            file=output,
        )
        print(f"Purpose: {procedure['purpose']}", file=output)
        for index, step in enumerate(procedure["steps"], 1):
            print(f"{index}. {step}", file=output)
        print(
            f"Evidence: {len(primitive['evidence'])} · "
            f"active skill changed: {'no' if patch['status'] in {'PROPOSED', 'APPROVED'} else 'yes'}",
            file=output,
        )
        if primitive.get("events"):
            print(
                "Lifecycle: "
                + " → ".join(event["event_type"] for event in primitive["events"]),
                file=output,
            )
        if primitive.get("observation_contract") is not None:
            contract = primitive["observation_contract"]
            latest = primitive.get("latest_assessment")
            print(
                f"Observation: {len(primitive['observations'])}/"
                f"{contract['minimum_observations']}..{contract['maximum_observations']} · "
                f"assessment={latest['decision'] if latest else 'not assessed'}",
                file=output,
            )
    elif args.company_command == "skill-assess":
        assessment = primitive["assessment"]
        print(
            f"{assessment['patch_id']} · {assessment['decision']} · "
            f"cohort={len(assessment['observation_ids'])} · "
            f"exposed={assessment['exposed_count']}",
            file=output,
        )
        print("Automatic rollback: disabled", file=output)
    elif args.company_command == "roster-patches":
        if not primitive:
            print("No company roster-patches yet.", file=output)
        for item in primitive:
            print(
                f"{item['patch_id']}  {item['status']}  "
                f"{item['operation']}  {item['employee_id']}",
                file=output,
            )
    elif args.company_command in {"episodes", "patches", "evidence-pairs"}:
        if not primitive:
            print(f"No company {args.company_command} yet.", file=output)
        for item in primitive:
            identifier = item.get(
                "episode_id", item.get("patch_id", item.get("pair_id", "unknown"))
            )
            label = item.get(
                "task_family",
                item.get("fixture", item.get("pattern", {}).get("task_family", "")),
            )
            status = item.get(
                "status", item.get("source", "VERIFIED" if "pair_id" in item else "")
            )
            print(f"{identifier}  {status}  {label}", file=output)
    elif args.company_command == "evidence-preview":
        pair = primitive["pair"]
        print(
            f"{pair['pair_id']} · VERIFIED · "
            f"{'importable' if primitive['importable'] else 'duplicate'}",
            file=output,
        )
        print(
            f"{pair['fixture']} · {pair['provider_kind']} · {pair['model_id']} · "
            f"revision={pair['source_revision']}",
            file=output,
        )
        print(
            f"Campaign: {pair['campaign_id']} · runs "
            f"{pair['baseline_run_id']} → {pair['dynamic_run_id']}",
            file=output,
        )
        print(
            f"Effect: quality {pair['baseline_quality_score']:.4f} → "
            f"{pair['dynamic_quality_score']:.4f} · model calls "
            f"{pair['baseline_model_calls']} → {pair['dynamic_model_calls']}",
            file=output,
        )
        print("State changed: no", file=output)
        for conflict in primitive["duplicate_conflicts"]:
            print(f"- import blocked: {conflict}", file=output)
    elif args.company_command == "evidence-import":
        pair = primitive["pair"]
        print(
            f"{pair['pair_id']} · imported as {primitive['episode']['episode_id']}",
            file=output,
        )
        print("Automatic curation/apply: disabled", file=output)
    elif args.company_command == "workflow-promote-preview":
        envelope = primitive["envelope"]
        patch = primitive["candidate"]
        print(
            f"{envelope['promotion_id']} · READY · "
            f"{'existing proposal' if primitive['proposal_exists'] else 'proposal available'}",
            file=output,
        )
        print(
            f"Pattern: {envelope['source_pattern_id']} → "
            f"{envelope['target_pattern_id']}",
            file=output,
        )
        print(
            f"Context: {envelope['target_context_fingerprint']} · "
            f"PLAYBOOK r{envelope['base_playbook_revision']}",
            file=output,
        )
        print(
            f"Effect: quality {envelope['quality_gain']:+.4f} · "
            f"model calls {envelope['model_call_delta']:+d} · "
            f"tokens {envelope['token_delta']:+d}",
            file=output,
        )
        print(
            f"Proposal: {patch['patch_id']} · PROPOSED only · "
            "approval/apply automatic=false",
            file=output,
        )
        print("State changed: no · provider calls: 0", file=output)
    elif args.company_command == "workflow-promote":
        patch = primitive["patch"]
        envelope = primitive["preview"]["envelope"]
        print(
            f"{patch['patch_id']} · {patch['status']} · "
            f"{'created' if primitive['created'] else 'reused'}",
            file=output,
        )
        print(
            f"Promotion: {envelope['promotion_id']} · "
            f"context={envelope['target_context_fingerprint']}",
            file=output,
        )
        print(
            f"COMPANY r{primitive['active_company_revision']} · "
            f"ROSTER r{primitive['active_roster_revision']} · "
            f"PLAYBOOK r{primitive['active_playbook_revision']} unchanged",
            file=output,
        )
        print("Approval/apply: separate explicit actions", file=output)
    elif args.company_command == "preview":
        patch = primitive["patch"]
        print(
            f"{patch['patch_id']} · {patch['status']} · "
            f"{'eligible' if patch['eligible_for_apply'] else 'preview only'}",
            file=output,
        )
        print(
            f"PLAYBOOK r{patch['base_playbook_revision']} → pattern "
            f"{patch['pattern']['pattern_id']} · evidence={len(patch['evidence_episode_ids'])}",
            file=output,
        )
        print(
            f"Expected: quality +{patch['expected_quality_gain']:.4f} · "
            f"model calls {patch['expected_model_call_savings']:+d} saved · "
            f"confidence={patch['confidence']:.2f}",
            file=output,
        )
        print(
            f"Match: {patch['pattern']['execution_profile']} · "
            f"context={patch['pattern']['context_fingerprint']}",
            file=output,
        )
        for task in patch["pattern"]["tasks"]:
            dependencies = ",".join(task["depends_on"]) or "ready"
            capabilities = ",".join(task["required_capabilities"])
            final = " · final" if task["final"] else ""
            print(
                f"- {task['task_key']} ← {dependencies} · {capabilities}{final}",
                file=output,
            )
        for reason in patch["ineligibility_reasons"]:
            print(f"- apply blocked: {reason}", file=output)
        if primitive["events"]:
            print(
                "Lifecycle: "
                + " → ".join(event["event_type"] for event in primitive["events"]),
                file=output,
            )
        contract = primitive["observation_contract"]
        if contract is not None:
            cohort_count = sum(
                item["cohort_eligible"] for item in primitive["observations"]
            )
            latest = primitive["latest_assessment"]
            print(
                f"Observation: cohort={cohort_count}/"
                f"{contract['minimum_observations']}..{contract['maximum_observations']} · "
                f"assessment={latest['decision'] if latest else 'not assessed'}",
                file=output,
            )
    elif args.company_command in {"roster-propose", "roster-preview"}:
        render_roster_patch_preview(primitive, output)
    elif args.company_command in {
        "roster-approve",
        "roster-apply",
        "roster-reject",
    }:
        print(
            f"{primitive['patch_id']} · {primitive['status']} · "
            f"ROSTER r{active_roster_revision}",
            file=output,
        )
    elif args.company_command in {
        "skill-approve",
        "skill-apply",
        "skill-reject",
        "skill-rollback",
    }:
        print(
            f"{primitive['patch_id']} · {primitive['status']} · "
            f"skill revision={primitive.get('applied_skill_revision') or primitive.get('rolled_back_skill_revision') or primitive['base_skill_revision']}",
            file=output,
        )
    else:
        print(
            f"{primitive['patch_id']} · {primitive['status']} · "
            f"PLAYBOOK r{active_playbook_revision}",
            file=output,
        )
    if args.company_command == "replay" and not primitive["replay_matches"]:
        return COMPANY_COMMAND_JOB_FAILED
    return COMPANY_COMMAND_OK
