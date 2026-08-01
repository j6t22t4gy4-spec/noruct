from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import replace
from datetime import datetime

from .models import (
    CompletionEnvelope,
    CompletionValidation,
    EmployeeRunRequest,
    EmployeeRunResult,
    EmployeeSessionRetention,
    EventType,
    Failure,
    FailureCategory,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamProgress,
    RunHandle,
    RunSignal,
    RunStatus,
    SignalCode,
    ToolCall,
    Usage,
    to_primitive,
    utc_now,
)
from .context_compaction import BoundedContextCompactor
from .cost_efficiency import CostEfficiencyProjector
from .ports import (
    ApprovalPort,
    CancellationToken,
    CompletionValidatorPort,
    ModelProviderError,
    ModelProviderPort,
    OperationCancelled,
    StreamingModelProviderPort,
)
from .prompt import PromptBuilder
from .store import (
    EmployeeSessionConflict,
    EmployeeSessionUpdate,
    RunStore,
    employee_session_namespace,
)
from .tool_batch import PermissionPreservingToolBatchPlanner, ToolBatchMode
from .tools import (
    PolicyDenied,
    ToolExecutionError,
    ToolExecutor,
    ToolRegistry,
    capability_projection,
)



class EmployeeAgentLoopOutcomeMixin:
    """Validate model responses and terminalize bounded Employee results."""
    @staticmethod
    def _response_error(response: ModelResponse, max_result_bytes: int) -> str | None:
        if response.completion and response.tool_calls:
            return "A model response cannot contain both completion and tool calls."
        if not response.completion and not response.tool_calls:
            return "A model response must contain a completion or at least one tool call."
        if response.completion:
            if not response.completion.summary.strip():
                return "Completion summary must be non-empty."
            encoded = json.dumps(
                to_primitive(response.completion),
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            if len(encoded) > max_result_bytes:
                return "Completion exceeds the result byte limit."
        seen: set[str] = set()
        for call in response.tool_calls:
            if not call.call_id or not call.name:
                return "Tool calls require non-empty call_id and name."
            if call.call_id in seen:
                return "Tool call IDs must be unique within a model response."
            seen.add(call.call_id)
            if not isinstance(call.arguments, dict):
                return "Tool call arguments must be an object."
            if "_provider_arguments_error" in call.arguments:
                return "Tool call arguments were rejected by the provider boundary."
        return None

    @classmethod
    def _completion_validation_error(
        cls,
        validation: CompletionValidation,
    ) -> str | None:
        if type(validation) is not CompletionValidation:
            return "Completion validator returned an unsupported result type."
        if type(validation.passed) is not bool:
            return "Completion validation passed must be boolean."
        if validation.passed:
            if validation.failed_checks or validation.semantic_expectation:
                return "Passing completion validation cannot contain failure feedback."
            return None
        if not 1 <= len(validation.failed_checks) <= 8:
            return "Failed completion validation requires one to eight checks."
        if len(set(validation.failed_checks)) != len(validation.failed_checks):
            return "Completion validation check names must be unique."
        if any(
            cls._VALIDATION_CHECK_PATTERN.fullmatch(check) is None
            for check in validation.failed_checks
        ):
            return "Completion validation check names are invalid."
        expectation = validation.semantic_expectation
        if (
            not expectation
            or len(expectation) > 512
            or any(ord(character) < 32 or ord(character) > 126 for character in expectation)
        ):
            return "Completion validation feedback must be bounded printable ASCII."
        return None

    @staticmethod
    def _model_call_ceiling(provider: ModelProviderPort) -> int:
        value = getattr(provider, "model_call_ceiling", 1)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return 1
        return value

    @staticmethod
    def _budget_reason(
        request: EmployeeRunRequest,
        usage: Usage,
        started_monotonic: float,
        *,
        before_model: bool,
    ) -> str | None:
        limits = request.limits
        if (time.monotonic() - started_monotonic) * 1000 >= limits.max_wall_time_ms:
            return "max_wall_time_ms"
        if before_model and usage.model_calls >= limits.max_model_calls:
            return "max_model_calls"
        if usage.input_tokens > limits.max_input_tokens or (
            before_model and usage.input_tokens >= limits.max_input_tokens
        ):
            return "max_input_tokens"
        if usage.output_tokens > limits.max_output_tokens or (
            before_model and usage.output_tokens >= limits.max_output_tokens
        ):
            return "max_output_tokens"
        if usage.cost_usd > limits.max_cost_usd or (
            limits.max_cost_usd > 0
            and before_model
            and usage.cost_usd >= limits.max_cost_usd
        ):
            return "max_cost_usd"
        return None

    def _base_result(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        status: RunStatus,
        usage: Usage,
        *,
        summary: str,
        artifacts: tuple[str, ...] = (),
        evidence: tuple[str, ...] = (),
        unresolved: tuple[str, ...] = (),
        observations: tuple[str, ...] = (),
        followups: tuple[str, ...] = (),
        signals: tuple[RunSignal, ...] = (),
        partial: bool,
        failure: Failure | None = None,
    ) -> EmployeeRunResult:
        row = self.store.get_run(handle.run_id) or {}
        started_at = datetime.fromisoformat(row["started_at"]) if row.get("started_at") else None
        return EmployeeRunResult(
            run_id=handle.run_id,
            request_id=handle.request_id,
            job_id=request.task.job_id,
            task_id=request.task.task_id,
            employee_id=request.employee.employee_id,
            status=status,
            summary=summary,
            output_artifact_refs=artifacts,
            acceptance_evidence=evidence,
            unresolved_issues=unresolved,
            observations=observations,
            suggested_followups=followups,
            signals=tuple(signals),
            partial_result=partial,
            usage=usage,
            last_event_seq=0,
            started_at=started_at,
            finished_at=utc_now(),
            failure=failure,
        )

    def _succeeded(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        usage: Usage,
        completion: CompletionEnvelope,
        *,
        employee_session: EmployeeSessionUpdate | None = None,
    ) -> EmployeeRunResult:
        result = self._base_result(
            request,
            handle,
            RunStatus.SUCCEEDED,
            usage,
            summary=completion.summary,
            artifacts=completion.artifact_refs,
            evidence=completion.acceptance_evidence,
            unresolved=completion.unresolved_issues,
            observations=completion.observations,
            followups=completion.suggested_followups,
            signals=completion.signals,
            partial=False,
        )
        return self.store.terminalize(
            result,
            EventType.RUN_SUCCEEDED,
            {"summary_bytes": len(completion.summary.encode("utf-8"))},
            employee_session=employee_session,
        )

    def _assignee_mismatch(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        usage: Usage,
        completion: CompletionEnvelope,
    ) -> EmployeeRunResult:
        failure = Failure(
            "ASSIGNEE_CAPABILITY_MISMATCH",
            FailureCategory.INPUT,
            "The assigned employee returned a typed assignment mismatch.",
            retryable=False,
        )
        result = self._base_result(
            request,
            handle,
            RunStatus.FAILED,
            usage,
            summary=completion.summary,
            unresolved=(
                *completion.unresolved_issues,
                "Kernel must select another frozen exact-capable employee or fail the task.",
            ),
            observations=completion.observations,
            followups=completion.suggested_followups,
            signals=completion.signals,
            partial=True,
            failure=failure,
        )
        return self.store.terminalize(
            result,
            EventType.RUN_FAILED,
            {
                "failure_code": failure.code,
                "category": failure.category.value,
                "signal": SignalCode.ASSIGNEE_MISMATCH.value,
            },
        )

    def _failed(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        usage: Usage,
        failure: Failure,
    ) -> EmployeeRunResult:
        existing = self.store.get_result(handle.run_id)
        if existing:
            return existing
        result = self._base_result(
            request,
            handle,
            RunStatus.FAILED,
            usage,
            summary=failure.message_safe,
            unresolved=("Kernel decision required.",),
            partial=True,
            failure=failure,
        )
        return self.store.terminalize(
            result,
            EventType.RUN_FAILED,
            {"failure_code": failure.code, "category": failure.category.value},
        )

    def _budget_exhausted(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        usage: Usage,
        reason: str,
    ) -> EmployeeRunResult:
        failure = Failure(
            "RUN_BUDGET_EXHAUSTED",
            FailureCategory.TIMEOUT if reason == "max_wall_time_ms" else FailureCategory.MODEL,
            f"Run limit reached: {reason}",
            retryable=True,
        )
        result = self._base_result(
            request,
            handle,
            RunStatus.BUDGET_EXHAUSTED,
            usage,
            summary=failure.message_safe,
            unresolved=("A new budget reservation or smaller task is required.",),
            partial=True,
            failure=failure,
        )
        return self.store.terminalize(
            result,
            EventType.RUN_BUDGET_EXHAUSTED,
            {"limit": reason},
        )

    def _cancelled(
        self,
        request: EmployeeRunRequest,
        handle: RunHandle,
        usage: Usage,
        reason: str,
    ) -> EmployeeRunResult:
        existing = self.store.get_result(handle.run_id)
        if existing:
            return existing
        failure = Failure(
            "RUN_CANCELLED",
            FailureCategory.CANCEL,
            reason or "Run cancelled",
            retryable=True,
        )
        result = self._base_result(
            request,
            handle,
            RunStatus.CANCELLED,
            usage,
            summary=failure.message_safe,
            unresolved=("Completed external actions are not rolled back.",),
            partial=True,
            failure=failure,
        )
        return self.store.terminalize(
            result,
            EventType.RUN_CANCELLED,
            {"reason": failure.message_safe},
        )
