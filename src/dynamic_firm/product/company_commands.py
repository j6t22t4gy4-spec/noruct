"""Reusable Product-Surface adapters for explicit Company CLI commands.

The public CLI owns argument parsing and remains the only process entrypoint.
This module owns the bounded, provider-free adapters that are also usable by a
future GUI/API surface: roster proposal normalization, roster preview rendering,
and the explicitly operator-owned curation loop.  It does not own COMPANY
state, Kernel authority, runtime budgets, or approval policy.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, TextIO

from dynamic_firm.company import (
    CompanyLearningService,
    CompanyStateStore,
    EvolutionAutonomyMode,
    RosterPatchOperation,
    RosterPatchService,
)
from dynamic_firm.kernel.models import EmployeeRecord


def parse_operator_timestamp(value: str | None) -> datetime | None:
    """Parse an explicit operator filter without accepting naive local time."""

    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("operator timestamp must be ISO-8601 with a UTC offset") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("operator timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def propose_roster_patch(
    args: argparse.Namespace,
    service: RosterPatchService,
) -> object:
    """Normalize one CLI proposal into the existing ROSTER proposal lifecycle."""

    operation = RosterPatchOperation(args.operation)
    capabilities = tuple(args.capability)
    if operation == RosterPatchOperation.ADD_EMPLOYEE:
        if not args.role or not capabilities:
            raise ValueError("ADD_EMPLOYEE requires --role and at least one --capability")
        if args.active is not None:
            raise ValueError("ADD_EMPLOYEE is active by default; --active is only for SET_ACTIVE")
        return service.propose_add_employee(
            EmployeeRecord(
                employee_id=args.employee_id,
                role=args.role,
                capabilities=capabilities,
                active=True,
                temporary=False,
                model_profile=args.model_profile,
            ),
            rationale=args.rationale,
            actor="user:cli",
        )
    if operation == RosterPatchOperation.SET_ACTIVE:
        if args.active is None:
            raise ValueError("SET_ACTIVE requires --active true or --active false")
        if args.role or capabilities:
            raise ValueError("SET_ACTIVE accepts only --employee-id, --active, and --rationale")
        return service.propose_set_active(
            args.employee_id,
            args.active == "true",
            rationale=args.rationale,
            actor="user:cli",
        )
    if operation == RosterPatchOperation.UPDATE_EMPLOYEE:
        if not args.role or not capabilities or args.active is None:
            raise ValueError(
                "UPDATE_EMPLOYEE requires --employee-id, --role, --capability, --active, and --rationale"
            )
        return service.propose_update_employee(
            EmployeeRecord(
                employee_id=args.employee_id,
                role=args.role,
                capabilities=capabilities,
                active=args.active == "true",
                temporary=False,
                model_profile=args.model_profile,
            ),
            rationale=args.rationale,
            actor="user:cli",
        )
    if args.role or args.active is not None:
        raise ValueError(
            "SET_CAPABILITIES accepts only --employee-id, --capability, and --rationale"
        )
    if not capabilities:
        raise ValueError("SET_CAPABILITIES requires at least one --capability")
    return service.propose_set_capabilities(
        args.employee_id,
        capabilities,
        rationale=args.rationale,
        actor="user:cli",
    )


def _roster_employee_summary(employee: dict[str, Any] | None) -> str:
    if employee is None:
        return "none"
    capabilities = ",".join(str(item) for item in employee.get("capabilities", ()))
    return (
        f"{employee.get('employee_id', 'unknown')} · "
        f"{employee.get('role', 'unknown')} · "
        f"active={str(employee.get('active', False)).lower()} · "
        f"capabilities={capabilities or 'none'}"
    )


def render_roster_patch_preview(payload: dict[str, Any], output: TextIO) -> None:
    """Render a stable, content-bounded proposal preview without applying it."""

    patch = payload["patch"]
    print(
        f"{patch['patch_id']} · {patch['status']} · {patch['operation']}",
        file=output,
    )
    print(
        f"ROSTER r{patch['base_roster_revision']} → proposed r"
        f"{patch['base_roster_revision'] + 1} · "
        f"active remains r{payload['active_roster_revision']}",
        file=output,
    )
    print("Before: " + _roster_employee_summary(patch["before_employee"]), file=output)
    print("After:  " + _roster_employee_summary(patch["after_employee"]), file=output)
    print(f"Rationale: {patch['rationale']}", file=output)
    print(
        f"Proposed by: {patch['proposed_by']} · content {patch['content_hash'][:12]}",
        file=output,
    )
    if payload.get("evidence"):
        print(
            f"Staffing evidence: {len(payload['evidence'])} · "
            f"{'production eligible' if payload['evidence_eligible_for_apply'] else 'preview only'}",
            file=output,
        )
        for evidence in payload["evidence"]:
            print(
                f"- {evidence['evidence_id']} · {evidence['capability']} · "
                f"job={evidence['job_id']} · safe={str(evidence['job_succeeded']).lower()}",
                file=output,
            )
    if payload.get("assessments"):
        assessment = payload["assessments"][-1]
        print(
            f"Hire assessment: {assessment['assessment_id']} · "
            f"{assessment['decision']}",
            file=output,
        )
    if payload.get("retention_reviews"):
        review = payload["retention_reviews"][-1]
        print(
            f"Retention review: {review['mode']} · {review['decision']}",
            file=output,
        )
    if payload["events"]:
        print(
            "Lifecycle: "
            + " → ".join(event["event_type"] for event in payload["events"]),
            file=output,
        )
    print("Active ROSTER changed: no", file=output)


def render_company_observability(
    command: str,
    primitive: object,
    output: TextIO,
) -> bool:
    """Render read-only Company observation commands and report whether handled.

    This keeps CLI output vocabulary stable while making the same projection
    boundary available to non-terminal product surfaces.  It accepts only the
    already-sanitized primitive form produced by the command dispatcher.
    """

    if command == "status":
        if not isinstance(primitive, Mapping):
            raise ValueError("Company status projection is malformed")
        summary = _mapping(primitive, "summary")
        company = _mapping(primitive, "company")
        policies = _mapping(company, "policies")
        patch_counts = _mapping(summary, "patch_counts")
        roster_patch_counts = _mapping(summary, "roster_patch_counts")
        skill_patch_counts = _mapping(summary, "employee_skill_patch_counts")
        print(
            "Company state · "
            f"COMPANY r{summary['company_revision']} · "
            f"ROSTER r{summary['roster_revision']} "
            f"({summary['active_employee_count']} active / "
            f"{summary['employee_count']} total) · "
            f"PLAYBOOK r{summary['playbook_revision']} "
            f"({summary['workflow_pattern_count']} patterns)",
            file=output,
        )
        print(
            f"Evidence: {summary['episode_count']} episode(s) · "
            f"staffing demands={summary['staffing_demand_count']} · "
            f"verified live pairs={summary['verified_live_pair_count']} · "
            f"workflow proposed={patch_counts.get('PROPOSED', 0)} · "
            f"applied={patch_counts.get('APPLIED', 0)}",
            file=output,
        )
        print(
            "Roster patches: "
            f"proposed={roster_patch_counts.get('PROPOSED', 0)} · "
            f"applied={roster_patch_counts.get('APPLIED', 0)} · "
            f"hire contracts={summary['hire_observation_contract_count']} · "
            f"observations={summary['hire_observation_count']} · "
            f"assessments={summary['hire_assessment_count']}",
            file=output,
        )
        print(
            "Evolution autonomy: "
            f"{policies.get('evolution_autonomy_mode', 'PROPOSE')} · "
            "running Jobs stay pinned · hard guards always on",
            file=output,
        )
        print(
            "Retention review: "
            f"{summary['retention_review_mode']} · "
            f"reviews={summary['retention_review_count']} · "
            "hard invariants always on",
            file=output,
        )
        print(
            "Employee skills: "
            f"active heads={summary['employee_skill_count']} · "
            f"proposed={skill_patch_counts.get('PROPOSED', 0)} · "
            f"applied={skill_patch_counts.get('APPLIED', 0)} · "
            f"observations={summary['employee_skill_observation_count']} · "
            "review=approval only",
            file=output,
        )
        return True
    if command == "manager-outcomes":
        if not isinstance(primitive, (tuple, list)):
            raise ValueError("Manager outcome projection is malformed")
        if not primitive:
            print("No Manager-attributed organization episodes yet.", file=output)
        for assessment in primitive:
            if not isinstance(assessment, Mapping):
                raise ValueError("Manager outcome item is malformed")
            p10 = assessment["p10_quality_delta"]
            model_delta = assessment["median_model_call_delta"]
            p10_label = "n/a" if p10 is None else f"{p10:+.3f}"
            model_delta_label = "n/a" if model_delta is None else f"{model_delta:+d}"
            print(
                f"{assessment['manager_employee_id']} · {assessment['decision']} · "
                f"episodes={len(assessment['observed_episode_ids'])} · "
                f"production={assessment['production_episode_count']} · "
                f"p10 quality={p10_label} · "
                f"median model calls={model_delta_label}",
                file=output,
            )
            print(
                "  "
                f"specialist={assessment['specialist_job_count']} · "
                f"replan={assessment['replan_job_count']} · "
                f"supervised={assessment['supervised_job_count']} · "
                f"negative-transfer={assessment['negative_transfer_count']} · "
                "automatic promotion: disabled",
                file=output,
            )
            for reason in assessment["reasons"]:
                print(f"  - {reason}", file=output)
        return True
    if command == "organization-metrics":
        if not isinstance(primitive, Mapping):
            raise ValueError("Organization metrics projection is malformed")
        print(
            "Organization evidence · "
            f"episodes={primitive['episode_count']} · "
            f"first-runnable observed={primitive['observed_time_to_first_runnable_count']}",
            file=output,
        )
        latency = primitive["median_time_to_first_runnable_ms"]
        print(
            "Median first-runnable: "
            + (f"{latency}ms" if latency is not None else "not observed"),
            file=output,
        )
        decisions = _mapping(primitive, "graph_proposal_decisions")
        print(
            "Graph proposal decisions: "
            f"approved={decisions.get('APPROVED', 0)} · "
            f"rejected={decisions.get('REJECTED', 0)} · "
            f"unavailable={decisions.get('UNAVAILABLE', 0)}",
            file=output,
        )
        print(
            "Read-only evidence · automatic graph, budget, and Patch changes: disabled",
            file=output,
        )
        return True
    if command == "organization-outcomes":
        if not isinstance(primitive, Mapping):
            raise ValueError("Organization outcome projection is malformed")
        assessments = primitive.get("assessments")
        if not isinstance(assessments, (tuple, list)):
            raise ValueError("Organization outcome assessments are malformed")
        if not assessments:
            print("No organization outcome context has production evidence yet.", file=output)
        for assessment in assessments:
            if not isinstance(assessment, Mapping):
                raise ValueError("Organization outcome assessment item is malformed")
            quality = assessment.get("lower_decile_quality_delta")
            calls = assessment.get("median_model_call_delta")
            quality_label = "n/a" if quality is None else f"{float(quality):+.3f}"
            calls_label = "n/a" if calls is None else f"{int(calls):+d}"
            print(
                f"{assessment['context_fingerprint']} · {assessment['decision']} · "
                f"production={assessment['production_episode_count']} · "
                f"team-baselines={assessment['baselined_team_episode_count']} · "
                f"replica-baselines={assessment['baselined_replica_episode_count']}",
                file=output,
            )
            print(
                f"  p10 quality={quality_label} · median model calls={calls_label} · "
                "application=next Job only",
                file=output,
            )
            for reason in assessment.get("reasons", ()):  # type: ignore[union-attr]
                print(f"  - {reason}", file=output)
        print("Read-only evidence · running Jobs, Graph, budget, and Patch state: unchanged", file=output)
        return True
    if command == "manager-report":
        if primitive is None:
            print("No persistent Manager is active; use manager-migrate to propose one.", file=output)
            return True
        if not isinstance(primitive, Mapping):
            raise ValueError("Manager report projection is malformed")
        print(
            f"Manager {primitive['manager_employee_id']} · ROSTER r{primitive['roster_revision']} · "
            f"model={primitive['model_profile']}",
            file=output,
        )
        print(
            f"Managed episodes={primitive['manager_episode_count']} · "
            f"supervised={primitive['supervised_job_count']} · "
            f"specialist={primitive['specialist_job_count']} · "
            f"replanned={primitive['replanned_job_count']}",
            file=output,
        )
        if primitive["pending_reason"]:
            print(f"Qualification pending: {primitive['pending_reason']}", file=output)
        for assessment in primitive["assessment"]:
            print(
                f"- {assessment['context_fingerprint']} · {assessment['decision']} · "
                f"p10 quality={assessment['p10_quality_delta'] if assessment['p10_quality_delta'] is not None else 'n/a'}",
                file=output,
            )
        return True
    return False


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise ValueError(f"Company observation field {key!r} is malformed")
    return nested


def run_company_curate_daemon(
    args: argparse.Namespace,
    *,
    state_path: Path,
    output: TextIO,
) -> int:
    """Run deterministic curation in an explicitly operator-owned foreground loop.

    This may create or reuse a proposal record exactly as ``company curate``
    does, but it never starts a provider, Company Job, schedule, network
    connection, detached background process, or automatic rollback.
    """

    if not args.confirm:
        raise ValueError(
            "Company curate-daemon requires --confirm because it can create evidence-backed patch proposals"
        )
    if not 30 <= args.poll_seconds <= 3600:
        raise ValueError("Company curate-daemon poll interval must be between 30 and 3600 seconds")
    if args.max_cycles is not None and not 1 <= args.max_cycles <= 10_000:
        raise ValueError("Company curate-daemon max_cycles must be between 1 and 10000")
    cycles: list[dict[str, object]] = []
    cycle_count = 0
    try:
        while args.max_cycles is None or cycle_count < args.max_cycles:
            with CompanyStateStore(state_path) as store:
                learning = CompanyLearningService(store)
                mode = store.evolution_autonomy_mode()
                result = learning.curate()
                applied: list[str] = []
                if mode == EvolutionAutonomyMode.ALWAYS_APPROVE:
                    for candidate in result.candidates:
                        if not candidate.eligible_for_apply:
                            continue
                        approved = learning.approve(
                            candidate.patch_id,
                            actor="user-policy:evolution-always-approve",
                        )
                        learning.apply(
                            approved.patch_id,
                            actor="user-policy:evolution-always-approve",
                        )
                        applied.append(candidate.patch_id)
            cycles.append(
                {
                    "cycle": cycle_count + 1,
                    "autonomy_mode": mode.value,
                    "decision": result.decision,
                    "considered_episode_count": result.considered_episode_count,
                    "qualified_episode_count": result.qualified_episode_count,
                    "candidate_count": len(result.candidates),
                    "automatically_applied_workflow_patch_ids": applied,
                    "reasons": list(result.reasons),
                }
            )
            cycle_count += 1
            if args.max_cycles is None or cycle_count < args.max_cycles:
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        pass
    record = {
        "curator": "foreground_operator_confirmed_deterministic_loop",
        "poll_seconds": args.poll_seconds,
        "cycles": cycles,
        "stopped": "terminal_interrupt_or_requested_cycle_limit",
        "automatic_approve": any(
            item["autonomy_mode"] == EvolutionAutonomyMode.ALWAYS_APPROVE.value
            for item in cycles
        ),
        "automatic_apply": any(bool(item["automatically_applied_workflow_patch_ids"]) for item in cycles),
        "automatic_rollback": False,
        "provider_calls": 0,
        "company_jobs_created": 0,
        "background_service": False,
    }
    if args.json:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2), file=output)
    else:
        print(f"Company curate daemon stopped · {len(cycles)} cycle(s) · foreground only", file=output)
        print("No provider call, Company Job, rollback, or detached background service was enabled.", file=output)
    return 0
