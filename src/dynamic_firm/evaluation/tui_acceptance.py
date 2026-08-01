from __future__ import annotations

import io
from dataclasses import dataclass
from enum import StrEnum

from dynamic_firm import __version__
from dynamic_firm.product.events import ProductEvent, ProductEventType
from dynamic_firm.product.terminal import strip_ansi
from dynamic_firm.product.tui import InlineTerminalUI
from dynamic_firm.runtime.models import ApprovalRequest, ToolEffect, ToolRisk, Usage


class TuiAcceptanceScenario(StrEnum):
    CONVERSATION = "conversation"
    SOLO = "solo"
    APPROVAL = "approval"


@dataclass(frozen=True, slots=True)
class TuiAcceptanceCheck:
    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class TuiAcceptanceRecord:
    schema_version: str
    scenario: TuiAcceptanceScenario
    terminal_width: int
    machine_passed: bool
    human_review_required: bool
    quota_consumed: bool
    checks: tuple[TuiAcceptanceCheck, ...]
    rendered: str


@dataclass(frozen=True, slots=True)
class _Status:
    value: str


@dataclass(frozen=True, slots=True)
class _Metrics:
    unique_employee_count: int
    maximum_parallelism: int
    usage: Usage


@dataclass(frozen=True, slots=True)
class _Result:
    status: _Status
    acceptance_evidence: tuple[str, ...]
    unresolved_issues: tuple[str, ...]
    metrics: _Metrics


def _event(
    event_type: ProductEventType,
    message: str,
    *,
    task_id: str = "",
    employee_id: str = "",
    **data: object,
) -> ProductEvent:
    return ProductEvent(
        event_type,
        message,
        job_id="job-tui-acceptance",
        task_id=task_id,
        employee_id=employee_id,
        data=data,
    )


def _result(
    *,
    staff: int = 1,
    parallelism: int = 1,
    evidence: tuple[str, ...] = (),
    tool_calls: int = 0,
) -> _Result:
    return _Result(
        status=_Status("SUCCEEDED"),
        acceptance_evidence=evidence,
        unresolved_issues=(),
        metrics=_Metrics(
            unique_employee_count=staff,
            maximum_parallelism=parallelism,
            usage=Usage(
                model_calls=max(1, staff),
                tool_calls=tool_calls,
                input_tokens=640,
                output_tokens=120,
            ),
        ),
    )


def _approval_request() -> ApprovalRequest:
    return ApprovalRequest(
        action_id="action-tui-acceptance",
        run_id="run-tui-acceptance",
        job_id="job-tui-acceptance",
        task_id="task-implement-change",
        employee_id="employee-engineer",
        tool_name="apply_workspace_change_set",
        effect=ToolEffect.WRITE,
        risk=ToolRisk.MEDIUM,
        resource_key="workspace:preview:calculator.py",
        preview=(
            "Preview only; no workspace will be changed.\n"
            "--- a/calculator.py\n"
            "+++ b/calculator.py\n"
            "@@ -1 +1 @@\n"
            "-return left / right\n"
            "+return 0 if right == 0 else left / right"
        ),
        allow_session=False,
    )


def _render_scenario(
    scenario: TuiAcceptanceScenario,
    *,
    width: int,
    plain: bool,
    color: bool,
) -> str:
    output = io.StringIO()
    ui = InlineTerminalUI(
        stdin=io.StringIO("1\n"),
        stdout=output,
        interactive=False,
        color=color,
        animations=False,
        plain=plain,
        terminal_width=width,
    )
    ui.banner(
        workspace="/preview/noruct-project",
        session_id=f"{scenario.value}-preview",
        model="offline-acceptance",
        provider="no provider call",
        authority="preview only · no mutation",
        version=__version__,
        roster_revision=2,
        active_employee_count=2,
        employee_roles=("Noruct Generalist", "Repository Analyst"),
        capabilities=(
            "conversation",
            "evidence synthesis",
            "general reasoning",
            "repository analysis",
        ),
        tools=("list", "read"),
    )

    if scenario == TuiAcceptanceScenario.CONVERSATION:
        ui.begin_goal("이름이 뭐야?")
        ui.handle_event(_event(ProductEventType.INPUT_ROUTED, "direct", route="CONVERSATION"))
        ui.handle_event(
            _event(ProductEventType.PLAN_ACCEPTED, "direct plan", mode="DIRECT", task_count=1)
        )
        ui.handle_event(
            _event(
                ProductEventType.EMPLOYEE_STARTED,
                "generalist started",
                task_id="task-direct-response",
                employee_id="employee-company-generalist",
            )
        )
        ui.handle_event(
            _event(
                ProductEventType.EMPLOYEE_FINISHED,
                "employee-company-generalist succeeded: direct-response",
                task_id="task-direct-response",
                employee_id="employee-company-generalist",
            )
        )
        ui.handle_event(_event(ProductEventType.JOB_FINISHED, "Company job succeeded", status="SUCCEEDED"))
        ui.answer("나는 목표를 받아 필요한 최소 조직을 구성하는 Noruct야.")
        ui.result_details(_result())
    elif scenario == TuiAcceptanceScenario.SOLO:
        ui.begin_goal("이 작은 저장소의 실패 테스트 원인을 근거와 함께 찾아줘")
        ui.handle_event(_event(ProductEventType.INPUT_ROUTED, "company goal", route="COMPANY_GOAL"))
        ui.handle_event(_event(ProductEventType.COMPILER_STARTED, "compile"))
        ui.handle_event(
            _event(ProductEventType.PLAN_ACCEPTED, "solo plan", mode="SOLO", task_count=1)
        )
        ui.handle_event(
            _event(
                ProductEventType.EMPLOYEE_STARTED,
                "repository analyst started",
                task_id="task-analyze-goal",
                employee_id="employee-repository-analyst",
            )
        )
        ui.handle_event(
            _event(
                ProductEventType.EMPLOYEE_FINISHED,
                "employee-repository-analyst succeeded: analyze-goal",
                task_id="task-analyze-goal",
                employee_id="employee-repository-analyst",
            )
        )
        ui.handle_event(_event(ProductEventType.JOB_FINISHED, "Company job succeeded", status="SUCCEEDED"))
        ui.answer("한 명의 Repository Analyst가 실패 원인과 검증 근거를 정리했습니다.")
        ui.result_details(_result(evidence=("calculator.py:1",)))
    else:
        ui.begin_goal("0으로 나누는 경우를 안전하게 처리하고 검증해줘")
        ui.handle_event(_event(ProductEventType.INPUT_ROUTED, "company goal", route="COMPANY_GOAL"))
        ui.handle_event(_event(ProductEventType.COMPILER_STARTED, "compile"))
        ui.handle_event(
            _event(ProductEventType.PLAN_ACCEPTED, "solo coding plan", mode="SOLO", task_count=1)
        )
        ui.handle_event(
            _event(
                ProductEventType.EMPLOYEE_STARTED,
                "engineer started",
                task_id="task-implement-change",
                employee_id="employee-engineer",
            )
        )
        ui.handle_event(
            _event(
                ProductEventType.VALIDATION_RECORDED,
                "fixture validation passed",
                task_id="task-implement-change",
                employee_id="employee-engineer",
                attempt=1,
                name="fixture-validation",
                passed=True,
                detail="3 bounded checks passed",
            )
        )
        ui.handle_event(_event(ProductEventType.APPROVAL_REQUIRED, "Review required"))
        ui.ask_approval(_approval_request())
        ui.commit("")
        ui.handle_event(
            _event(
                ProductEventType.APPROVAL_RESOLVED,
                "Approval allow once",
                task_id="task-implement-change",
                employee_id="employee-engineer",
            )
        )
        ui.handle_event(
            _event(
                ProductEventType.TOOL_FINISHED,
                "apply_workspace_change_set completed",
                task_id="task-implement-change",
                employee_id="employee-engineer",
                tool_name="apply_workspace_change_set",
                output_bytes=64,
            )
        )
        ui.handle_event(
            _event(
                ProductEventType.EMPLOYEE_FINISHED,
                "employee-engineer succeeded: implement-change",
                task_id="task-implement-change",
                employee_id="employee-engineer",
            )
        )
        ui.handle_event(_event(ProductEventType.JOB_FINISHED, "Company job succeeded", status="SUCCEEDED"))
        ui.answer("검증된 변경을 승인 후 적용했습니다.")
        ui.result_details(
            _result(
                evidence=("Applied shadow change: calculator.py",),
                tool_calls=1,
            )
        )
    ui.close()
    return output.getvalue()


def _checks(scenario: TuiAcceptanceScenario, rendered: str) -> tuple[TuiAcceptanceCheck, ...]:
    clean = strip_ansi(rendered)
    activity_unwrapped = clean.replace("\n│  ", " ")
    if scenario == TuiAcceptanceScenario.CONVERSATION:
        return (
            TuiAcceptanceCheck(
                "compiler-hidden",
                "Compiler" not in clean,
                "Direct conversation contains no Compiler row.",
            ),
            TuiAcceptanceCheck(
                "activity-hidden",
                "Company plan" not in clean,
                "Direct conversation contains no company activity tree.",
            ),
            TuiAcceptanceCheck(
                "answer-visible",
                "● Noruct" in clean
                or "나는 목표를 받아 필요한 최소 조직을 구성하는 Noruct야." in clean,
                "The direct answer is rendered in the Noruct response lane.",
            ),
        )
    if scenario == TuiAcceptanceScenario.SOLO:
        return (
            TuiAcceptanceCheck(
                "solo-plan-visible",
                "Plan · solo · 1 task" in clean,
                "The Compiler result identifies one SOLO task.",
            ),
            TuiAcceptanceCheck(
                "one-employee-visible",
                activity_unwrapped.count("◇ repository analyst · analyze goal") == 1,
                "Exactly one employee start row is visible.",
            ),
            TuiAcceptanceCheck(
                "no-parallel-claim",
                "· 1 parallel" not in clean,
                "The result footer does not mislabel one-worker execution as parallel.",
            ),
        )
    validation = clean.find("VALIDATION · PASS")
    approval = clean.find("APPROVAL · REQUIRED")
    applied = clean.find("TOOL · DONE")
    result = clean.find("● Noruct")
    if result < 0:
        result = clean.find("검증된 변경을 승인 후 적용했습니다.")
    return (
        TuiAcceptanceCheck(
            "decision-default-safe",
            "Enter defaults to deny" in clean,
            "The approval card keeps default deny visible.",
        ),
        TuiAcceptanceCheck(
            "diff-visible",
            all(marker in clean for marker in ("--- a/calculator.py", "+++ b/calculator.py", "@@ -1 +1 @@")),
            "The unified diff header and hunk are visible.",
        ),
        TuiAcceptanceCheck(
            "execution-order",
            -1 < validation < approval < applied < result,
            "Validation precedes approval, apply, and the final result.",
        ),
        TuiAcceptanceCheck(
            "change-visible",
            "changed  calculator.py" in clean,
            "The applied path is visible after the result.",
        ),
    )


def run_tui_acceptance(
    scenario: TuiAcceptanceScenario | str,
    *,
    width: int = 80,
    plain: bool = False,
    color: bool = False,
) -> TuiAcceptanceRecord:
    scenario = TuiAcceptanceScenario(scenario)
    if not 40 <= width <= 120:
        raise ValueError("TUI acceptance width must be between 40 and 120")
    rendered = _render_scenario(scenario, width=width, plain=plain, color=color)
    checks = _checks(scenario, rendered)
    return TuiAcceptanceRecord(
        schema_version="noruct.tui-acceptance.v1",
        scenario=scenario,
        terminal_width=width,
        machine_passed=all(check.passed for check in checks),
        human_review_required=True,
        quota_consumed=False,
        checks=checks,
        rendered=rendered,
    )
