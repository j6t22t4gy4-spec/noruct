"""Bounded read models for the persistent Executive Manager.

These tools do not give the Manager Company authority.  They project the
smallest decision-relevant subset of state that already belongs to the
Company, ACTIVE JOB, and Intent/Decision planes.  Raw transcripts, tool
payloads, credentials, approval tokens, and Knowledge evidence bodies never
cross this boundary.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.company.store import CompanyStateStore
from dynamic_firm.knowledge.store import KnowledgeStore, knowledge_state_path

from .company_budget import CompanyCostBudgetPolicy
from .job_ledger import ActiveJobInspector
from .manager_tool_policy import is_manager_tool
from .models import IdempotencyMode, ToolEffect, ToolRisk, to_primitive
from .ports import CancellationToken
from .store import RunStore
from .tools import ToolDefinition, ToolValidationError


_VISIBLE_COMPANY_POLICIES = frozenset(
    {
        "high_cost_or_irreversible_requires_user_approval",
        "roster_retention_review_mode",
        "evolution_autonomy_mode",
        "company_cost_budget",
    }
)


def _json(value: object) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _empty(arguments: Mapping[str, Any], tool_name: str) -> Mapping[str, Any]:
    if arguments:
        raise ToolValidationError(f"{tool_name} does not accept arguments")
    return {}


def _limit(arguments: Mapping[str, Any], tool_name: str, *, default: int = 5) -> Mapping[str, Any]:
    if set(arguments) - {"limit"}:
        raise ToolValidationError(f"{tool_name} received an unknown argument")
    value = arguments.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
        raise ToolValidationError("limit must be an integer between 1 and 8")
    return {"limit": value}


def _clip(value: str, maximum_bytes: int) -> tuple[str, bool]:
    encoded = value.strip().encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return encoded.decode("utf-8"), False
    clipped = encoded[:maximum_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8").rstrip(), True
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return "", True


class ManagerRuntimeTools:
    """One Job-bound, read-only catalog available only to its Manager."""

    def __init__(
        self,
        *,
        company_store: CompanyStateStore,
        run_store: RunStore,
        runtime_state_path: Path,
        current_job_id: str,
    ) -> None:
        self._company_store = company_store
        self._run_store = run_store
        self._knowledge_path = knowledge_state_path(runtime_state_path)
        self._current_job_id = current_job_id

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (
            self._company_definition(),
            self._job_definition(),
            self._intent_definition(),
            self._outcomes_definition(),
        )

    def _company_definition(self) -> ToolDefinition:
        async def handle(_arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()

            def project() -> Mapping[str, object]:
                company = self._company_store.company()
                roster = self._company_store.roster()
                summary = self._company_store.summary()
                budget_policy = CompanyCostBudgetPolicy.from_mapping(
                    company.policies.get("company_cost_budget")
                )
                return {
                    "schema": "noruct.manager-company-brief.v1",
                    "company": {
                        "revision": company.revision,
                        "purpose": _clip(company.purpose, 2_000)[0],
                        "policies": {
                            key: company.policies[key]
                            for key in sorted(_VISIBLE_COMPANY_POLICIES)
                            if key in company.policies
                        },
                    },
                    "roster": {
                        "revision": roster.revision,
                        "employees": tuple(
                            {
                                "employee_id": str(employee.get("employee_id", "")),
                                "role": str(employee.get("role", "")),
                                "capabilities": tuple(
                                    str(item) for item in employee.get("capabilities", ())
                                ),
                                "active": employee.get("active") is True,
                                "temporary": employee.get("temporary") is True,
                            }
                            for employee in roster.employees
                        ),
                    },
                    "organization": {
                        "playbook_revision": summary.playbook_revision,
                        "workflow_pattern_count": summary.workflow_pattern_count,
                        "episode_count": summary.episode_count,
                        "staffing_demand_count": summary.staffing_demand_count,
                    },
                    "budget": self._run_store.company_budget_status(budget_policy),
                    "authority_granted": False,
                }

            return _json(await asyncio.to_thread(project))

        name = "manager_inspect_company"
        return ToolDefinition(
            name=name,
            description=(
                "Read the current Company purpose, safe policy subset, persistent ROSTER "
                "capabilities, organization counters, and Company budget summary. This is an "
                "authority-free projection and cannot change policy, staffing, or budget."
            ),
            input_schema={"type": "object", "additionalProperties": False},
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=lambda arguments: _empty(arguments, name),
            resource_key=lambda _arguments: "manager:company",
            handler=handle,
            output_limit_bytes=24_000,
            parallel_safe=True,
        )

    def _job_definition(self) -> ToolDefinition:
        async def handle(_arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()

            def project() -> Mapping[str, object]:
                rows = self._run_store.get_job_ledger_rows(self._current_job_id)
                if rows is None:
                    # DIRECT work deliberately has no ACTIVE JOB graph.
                    return {
                        "schema": "noruct.manager-job-brief.v1",
                        "job_id": self._current_job_id,
                        "tracked_active_job": False,
                        "reason": "DIRECT_OR_NOT_YET_TRACKED",
                        "authority_granted": False,
                    }
                inspection = ActiveJobInspector(self._run_store).inspect(self._current_job_id)
                return {
                    "schema": "noruct.manager-job-brief.v1",
                    "job_id": inspection.job_id,
                    "tracked_active_job": True,
                    "work_mode": inspection.company_work_mode,
                    "coordination_policy": inspection.coordination_policy,
                    "requested_effect": inspection.requested_effect,
                    "audit_status": inspection.audit_status,
                    "job_status": inspection.job_status,
                    "attempt_count": inspection.attempt_count,
                    "mutation_count": inspection.mutation_count,
                    "graph_patch_count": inspection.graph_patch_count,
                    "final_graph_version": inspection.final_graph_version,
                    "tasks": inspection.reconstructed_tasks,
                    "runtime_runs": inspection.runtime_runs,
                    "errors": inspection.errors[:8],
                    "authority_granted": False,
                }

            return _json(await asyncio.to_thread(project))

        name = "manager_inspect_current_job"
        return ToolDefinition(
            name=name,
            description=(
                "Read a privacy-bounded status projection for this Manager assignment's current "
                "Job. It exposes lifecycle and task state, never prompts, transcripts, tool "
                "payloads, approval tokens, or a resume command."
            ),
            input_schema={"type": "object", "additionalProperties": False},
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=lambda arguments: _empty(arguments, name),
            resource_key=lambda _arguments: f"manager:job:{self._current_job_id}",
            handler=handle,
            output_limit_bytes=24_000,
            parallel_safe=True,
        )

    def _intent_definition(self) -> ToolDefinition:
        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            limit = int(arguments["limit"])

            def project() -> Mapping[str, object]:
                if not self._knowledge_path.exists():
                    return {
                        "schema": "noruct.manager-intent-brief.v1",
                        "intents": (),
                        "decisions": (),
                        "due_decisions": (),
                        "knowledge_runtime_present": False,
                        "authority_granted": False,
                    }
                store = KnowledgeStore(self._knowledge_path)
                try:
                    intents = store.list_intents(limit=limit)
                    decisions = store.list_decisions(limit=limit)
                    due = store.due_decisions(limit=limit)
                    return {
                        "schema": "noruct.manager-intent-brief.v1",
                        "knowledge_runtime_present": True,
                        "intents": tuple(
                            {
                                "intent_id": item.intent_id,
                                "goal": _clip(item.goal, 1_500)[0],
                                "priority": item.priority,
                                "status": item.status,
                                "constraints": tuple(_clip(value, 500)[0] for value in item.constraints[:8]),
                                "acceptance_criteria": tuple(
                                    _clip(value, 500)[0] for value in item.acceptance_criteria[:8]
                                ),
                                "revision": item.revision,
                                "updated_at": item.updated_at,
                            }
                            for item in intents
                        ),
                        "decisions": tuple(
                            {
                                "decision_id": item.decision_id,
                                "statement": _clip(item.statement, 1_500)[0],
                                "status": item.status,
                                "intent_id": item.intent_id,
                                "review_at": item.review_at,
                                "revision": item.revision,
                                "updated_at": item.updated_at,
                            }
                            for item in decisions
                        ),
                        "due_decisions": tuple(item.decision_id for item in due),
                        "authority_granted": False,
                    }
                finally:
                    store.close()

            return _json(await asyncio.to_thread(project))

        name = "manager_read_intent_brief"
        return ToolDefinition(
            name=name,
            description=(
                "Read a bounded Intent and Decision control-plane brief. It returns goals, "
                "constraints, acceptance criteria, decision statements, and review timing; it "
                "does not return Knowledge bodies or permit decision changes."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 8}},
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=lambda arguments: _limit(arguments, name),
            resource_key=lambda _arguments: "manager:intent",
            handler=handle,
            output_limit_bytes=24_000,
            parallel_safe=True,
        )

    def _outcomes_definition(self) -> ToolDefinition:
        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            limit = int(arguments["limit"])

            def project() -> Mapping[str, object]:
                inspector = ActiveJobInspector(self._run_store)
                outcomes = []
                for summary in inspector.list(limit):
                    inspection = inspector.inspect(summary.job_id)
                    terminal = dict(inspection.terminal or {})
                    outcomes.append(
                        {
                            "job_id": summary.job_id,
                            "work_order_id": summary.work_order_id,
                            "work_mode": summary.company_work_mode,
                            "requested_effect": summary.requested_effect,
                            "audit_status": summary.audit_status,
                            "job_status": summary.job_status,
                            "created_at": summary.created_at,
                            "attempt_count": summary.attempt_count,
                            "mutation_count": summary.mutation_count,
                            "graph_patch_count": summary.graph_patch_count,
                            "failure_reason": str(terminal.get("failure_reason", ""))[:256],
                            "metrics": terminal.get("metrics", {}),
                        }
                    )
                return {
                    "schema": "noruct.manager-outcome-brief.v1",
                    "outcomes": tuple(outcomes),
                    "newest_first": True,
                    "raw_employee_output_included": False,
                    "authority_granted": False,
                }

            return _json(await asyncio.to_thread(project))

        name = "manager_review_recent_outcomes"
        return ToolDefinition(
            name=name,
            description=(
                "Review bounded recent Company Job outcomes and aggregate metrics. Raw employee "
                "output, transcripts, and tool payloads are excluded, and no Job can be resumed "
                "or changed through this tool."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 8}},
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=lambda arguments: _limit(arguments, name),
            resource_key=lambda _arguments: "manager:outcomes",
            handler=handle,
            output_limit_bytes=24_000,
            parallel_safe=True,
        )
