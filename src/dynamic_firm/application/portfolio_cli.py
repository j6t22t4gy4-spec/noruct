"""Surface-neutral operator projection for the local Work Order portfolio.

The adapter deliberately stops before execution.  It exposes the operator
decisions that are already durable (policy, admission, settlement, and the
Manager campaign gate) without inventing a background scheduler or a second
Company budget authority.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, TextIO

from dynamic_firm.company import (
    ManagerCampaignQualification,
    PortfolioExecutionService,
    PortfolioPolicy,
    PortfolioReestimateChoice,
    WorkOrderPortfolioStore,
)
from dynamic_firm.evaluation.manager_value_campaign import (
    create_manager_value_campaign_report,
)
from dynamic_firm.runtime.models import to_primitive


def _policy_payload(policy: PortfolioPolicy) -> Mapping[str, object]:
    return {
        "schema": "noruct.portfolio-policy.v1",
        "max_active_jobs": policy.max_active_jobs,
        "max_reserved_cost_usd": policy.max_reserved_cost_usd,
        "max_incremental_model_calls": policy.max_incremental_model_calls,
        "max_incremental_tool_calls": policy.max_incremental_tool_calls,
        "max_incremental_cost_usd": policy.max_incremental_cost_usd,
        "capability_slots": dict(policy.capability_slots),
    }


def _capability_slots(values: list[str]) -> tuple[tuple[str, int], ...]:
    parsed: dict[str, int] = {}
    for value in values:
        capability, separator, raw_count = value.partition("=")
        if not separator or capability in parsed:
            raise ValueError("Portfolio capability slots require unique CAPABILITY=COUNT values")
        try:
            parsed[capability] = int(raw_count)
        except ValueError as error:
            raise ValueError("Portfolio capability slot count is invalid") from error
    return tuple(sorted(parsed.items()))


def _campaign_report(directory: Path | None) -> object | None:
    return (
        None
        if directory is None
        else create_manager_value_campaign_report(directory)
    )


def _render(payload: object, *, as_json: bool, output: TextIO, summary: str) -> None:
    if as_json:
        print(json.dumps(to_primitive(payload), ensure_ascii=False, sort_keys=True, indent=2), file=output)
        return
    print(summary, file=output)


def run_portfolio_command(
    args: argparse.Namespace,
    *,
    state_path: Path,
    output: TextIO,
    company_episodes: Callable[[Path], tuple[object, ...]],
    drain: Callable[[PortfolioPolicy], object] | None = None,
    submit: Callable[
        [int, float | None, tuple[str, ...], datetime | None, tuple[str, ...]],
        object,
    ]
    | None = None,
) -> int:
    """Run one parsed portfolio command without reading Work Order content.

    A preview can reconcile the deterministic *local* queue, but never makes
    a Company-budget lease or starts a Job.  This keeps it a safe preparation
    action for CLI, TUI, and a later GUI.
    """

    portfolio_path = state_path.with_name(f"{state_path.stem}.work-orders.db")
    if args.portfolio_command == "campaign-gate":
        report = _campaign_report(args.directory)
        qualification = ManagerCampaignQualification.from_report(report)
        payload = {
            "schema": "noruct.portfolio-manager-campaign-gate.v1",
            "automatic_reuse_allowed": qualification.automatic_reuse_allowed,
            "qualified": qualification.qualified,
            "reasons": qualification.reasons,
        }
        _render(
            payload,
            as_json=args.json,
            output=output,
            summary=(
                "Manager Blueprint automatic reuse allowed."
                if qualification.automatic_reuse_allowed
                else "Manager Blueprint automatic reuse blocked · "
                + ", ".join(qualification.reasons)
            ),
        )
        return 0

    with WorkOrderPortfolioStore(portfolio_path) as store:
        if args.portfolio_command == "reestimate":
            if args.portfolio_reestimate_command == "report":
                if not args.confirm:
                    raise ValueError("Portfolio re-estimate report requires --confirm")
                notice = store.report_reestimate(
                    args.work_order_id,
                    proposed_reserved_cost_usd=args.proposed_reserved_cost_usd,
                    reason=args.reason,
                )
                payload = {
                    "schema": "noruct.portfolio-reestimate.v1",
                    "notice": to_primitive(notice),
                    "runtime_action": "NONE",
                    "next_action": "EXPLICIT_USER_DECISION_REQUIRED",
                }
                _render(
                    payload, as_json=args.json, output=output,
                    summary="Portfolio re-estimate recorded · no Job was paused, cancelled, or changed.",
                )
                return 0
            if args.portfolio_reestimate_command == "decide":
                notice = store.decide_reestimate(
                    args.reestimate_id,
                    choice=PortfolioReestimateChoice(args.choice),
                    reason=args.reason,
                    confirmed=args.confirm,
                )
                next_action = {
                    PortfolioReestimateChoice.CONTINUE: "NO_RUNTIME_MUTATION",
                    PortfolioReestimateChoice.REDUCE: "EXPLICIT_REPLACEMENT_WORK_ORDER_REQUIRED",
                    PortfolioReestimateChoice.CANCEL: "EXISTING_TERMINAL_CANCELLATION_PATH_REQUIRED",
                }[notice.choice]
                payload = {
                    "schema": "noruct.portfolio-reestimate.v1",
                    "notice": to_primitive(notice),
                    "runtime_action": "NONE",
                    "next_action": next_action,
                }
                _render(
                    payload, as_json=args.json, output=output,
                    summary=f"Portfolio re-estimate choice recorded · {notice.choice.value} · runtime unchanged.",
                )
                return 0
            payload = {
                "schema": "noruct.portfolio-reestimate-list.v1",
                "notices": store.reestimate_projection(),
                "runtime_action": "NONE",
            }
            _render(
                payload, as_json=args.json, output=output,
                summary=f"Portfolio re-estimates · notices={len(payload['notices'])} · runtime unchanged.",
            )
            return 0
        if args.portfolio_command == "submit":
            if not args.confirm:
                raise ValueError("Portfolio Work Order submission requires --confirm")
            if submit is None:
                raise RuntimeError("Portfolio Work Order submission is not composed")
            deadline = None
            if getattr(args, "deadline", None) is not None:
                try:
                    deadline = datetime.fromisoformat(
                        str(args.deadline).replace("Z", "+00:00")
                    )
                except ValueError as error:
                    raise ValueError("Portfolio deadline is not valid ISO-8601") from error
            entry = submit(
                args.priority,
                args.reserved_cost_usd,
                tuple(sorted(getattr(args, "depends_on", ()))),
                deadline,
                tuple(sorted(getattr(args, "requires_capability", ()))),
            )
            payload = {
                "schema": "noruct.portfolio-submission.v1",
                "entry": to_primitive(entry),
                "execution": "NOT_STARTED",
            }
            _render(
                payload,
                as_json=args.json,
                output=output,
                summary=(
                    f"Portfolio Work Order submitted · {entry.work_order_id} · "
                    f"priority={entry.priority} · execution not started"
                ),
            )
            return 0
        if args.portfolio_command == "policy":
            if args.portfolio_policy_command == "set":
                if not args.confirm:
                    raise ValueError("Portfolio policy change requires --confirm")
                policy = store.save_portfolio_policy(
                    PortfolioPolicy(
                        max_active_jobs=args.max_active_jobs,
                        max_reserved_cost_usd=args.max_reserved_cost_usd,
                        max_incremental_model_calls=args.max_incremental_model_calls,
                        max_incremental_tool_calls=args.max_incremental_tool_calls,
                        max_incremental_cost_usd=args.max_incremental_cost_usd,
                        capability_slots=_capability_slots(
                            list(getattr(args, "capability_slot", ()))
                        ),
                    )
                )
            else:
                policy = store.portfolio_policy()
            payload = _policy_payload(policy)
            _render(
                payload,
                as_json=args.json,
                output=output,
                summary=(
                    "Portfolio policy saved for future admission only."
                    if args.portfolio_policy_command == "set"
                    else "Portfolio local admission policy."
                ),
            )
            return 0

        policy = store.portfolio_policy()
        if args.portfolio_command == "drain":
            if not args.confirm:
                raise ValueError("Portfolio drain requires --confirm")
            if drain is None:
                raise RuntimeError("Portfolio Front Door drain is not composed")
            result = drain(policy)
            payload = {
                "schema": "noruct.portfolio-drain.v1",
                "policy": _policy_payload(policy),
                "result": to_primitive(result),
            }
            _render(
                payload,
                as_json=args.json,
                output=output,
                summary=(
                    "Portfolio bounded drain completed · "
                    f"waves={result.waves} · settled={len(result.settled_job_ids)} · "
                    f"blocked={len(result.blocked_job_ids)}"
                ),
            )
            return 0 if not result.blocked_job_ids else 4
        if args.portfolio_command == "status":
            payload = {
                "schema": "noruct.portfolio-status.v1",
                "policy": _policy_payload(policy),
                "entries": store.operator_projection(),
                "incremental_leases": store.incremental_lease_projection(),
                "settlements": store.settlement_projection(),
                "reestimates": store.reestimate_projection(),
            }
            _render(
                payload,
                as_json=args.json,
                output=output,
                summary=(
                    "Portfolio status · "
                    f"entries={len(payload['entries'])} · "
                    f"settlements={len(payload['settlements'])} · no daemon running"
                ),
            )
            return 0

        if args.portfolio_command == "preview":
            report = _campaign_report(args.manager_campaign_directory)
            plan = PortfolioExecutionService(store).next_dispatch_plan(
                policy=policy,
                episodes=company_episodes(state_path),
                context_fingerprint=args.context_fingerprint,
                manager_employee_id=args.manager_employee_id,
                automatic_blueprint_requested=args.automatic_blueprint_requested,
                manager_campaign_report=report,
            )
            payload = {
                "schema": "noruct.portfolio-preview.v1",
                "policy": _policy_payload(policy),
                "next_entry": None if plan.entry is None else to_primitive(plan.entry),
                "reuse_decision": plan.reuse_decision.value,
                "reasons": plan.reasons,
                "organization_assessment": to_primitive(plan.organization_assessment),
                "manager_assessment": (
                    None if plan.manager_assessment is None else to_primitive(plan.manager_assessment)
                ),
                "execution": "NOT_STARTED",
            }
            _render(
                payload,
                as_json=args.json,
                output=output,
                summary=(
                    "Portfolio preview · "
                    f"reuse={plan.reuse_decision.value} · "
                    + (
                        f"next={plan.entry.work_order_id}"
                        if plan.entry is not None
                        else "no unbound admitted Work Order"
                    )
                ),
            )
            return 0
    raise ValueError(f"Unknown portfolio command: {args.portfolio_command}")


__all__ = ["run_portfolio_command"]
