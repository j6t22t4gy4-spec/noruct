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



class EmployeeAgentLoopSessionMixin:
    """Own Employee conversation namespace and persistence projections.

    The canonical RunStore remains the state authority; this mixin only adapts
    the bounded session projection at the agent-loop boundary.
    """
    @staticmethod
    def _session_key(request: EmployeeRunRequest) -> str:
        """Return the profile-independent employee conversation namespace."""

        continuity = request.session_key.strip() or request.request_id
        return employee_session_namespace(request.employee.employee_id, continuity)

    @classmethod
    def _session_history_to_model_messages(
        cls,
        history: tuple[dict[str, object], ...],
    ) -> tuple[ModelMessage, ...]:
        """Adapt the shared JSON session projection to native model messages.

        The Employee Runtime capsule emits OpenAI-shaped dictionaries.  The
        native loop keeps strongly typed ``ModelMessage`` values, so this
        deliberately small adapter is the only profile boundary.  Malformed
        stored messages are rejected rather than rendered into a provider
        request with a changed meaning.
        """

        messages: list[ModelMessage] = []
        for raw in history:
            role = raw.get("role")
            if role not in {"user", "assistant", "tool"}:
                raise ValueError("employee session contains an unsupported message role")
            if "content" not in raw:
                raise ValueError("employee session message is missing content")
            tool_call_id = raw.get("tool_call_id")
            if tool_call_id is not None and not isinstance(tool_call_id, str):
                raise ValueError("employee session tool_call_id must be a string")
            content = raw["content"]
            if role == "assistant":
                # The capsule records provider-shaped tool calls.  Native
                # providers consume the first-party ToolCall projection in
                # ``content``; retain the chain instead of turning a prior
                # tool result into an orphaned message on profile handoff.
                calls = cls._provider_tool_calls_to_native(raw)
                if calls:
                    content = {
                        "content": content,
                        "tool_calls": calls,
                        "completion": None,
                    }
            messages.append(ModelMessage(str(role), content, tool_call_id))
        return tuple(messages)

    @staticmethod
    def _provider_tool_calls_to_native(raw: dict[str, object]) -> list[dict[str, object]]:
        """Normalize a portable OpenAI-shaped tool chain for provider ports."""

        candidates = raw.get("tool_calls")
        if not isinstance(candidates, list):
            return []
        calls: list[dict[str, object]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            call_id = candidate.get("id")
            function = candidate.get("function")
            if not isinstance(call_id, str) or not isinstance(function, dict):
                continue
            name = function.get("name")
            encoded_arguments = function.get("arguments")
            if not isinstance(name, str) or not isinstance(encoded_arguments, str):
                continue
            try:
                arguments = json.loads(encoded_arguments)
            except json.JSONDecodeError:
                raise ValueError("employee session tool arguments are invalid JSON") from None
            if not isinstance(arguments, dict):
                raise ValueError("employee session tool arguments must be an object")
            calls.append(
                {"call_id": call_id, "name": name, "arguments": arguments}
            )
        return calls

    def _canonical_messages(
        self,
        run_id: str,
        session_history: tuple[ModelMessage, ...],
        *,
        transient_user_message: str | None = None,
    ) -> tuple[ModelMessage, ...]:
        """Combine a stable prompt, shared history, and the active run ledger."""

        run_messages = tuple(self.store.list_messages(run_id))
        if transient_user_message is not None:
            projected = list(run_messages)
            first_user = next(
                (index for index, message in enumerate(projected) if message.role == "user"),
                None,
            )
            if first_user is None:
                raise RuntimeError("run-only evidence request is missing its user prompt record")
            projected[first_user] = ModelMessage("user", transient_user_message)
            run_messages = tuple(projected)
        if not session_history:
            return run_messages
        if not run_messages or run_messages[0].role != "system":
            # A durable approval resume and a normal run both retain the
            # initial system record.  Fail closed if the immutable ledger was
            # corrupted instead of accidentally sending history without the
            # frozen employee contract.
            raise RuntimeError("employee run is missing its system prompt record")
        return (run_messages[0], *session_history, *run_messages[1:])

    def _session_messages_for_success(
        self,
        session_history: tuple[ModelMessage, ...],
        run_id: str,
    ) -> tuple[dict[str, object], ...]:
        """Produce the portable profile-neutral history committed on success.

        We never persist the per-run system prompt: it is regenerated from
        the frozen employee snapshot for every runtime and every profile.
        Assistant and tool records use standard provider-shaped fields so the
        capsule can replay a native turn and the native loop can replay a
        capsule turn without importing the other implementation.
        """

        projected: list[dict[str, object]] = [
            self._model_message_to_session_message(message)
            for message in session_history
        ]
        for message in self.store.list_messages(run_id):
            if message.role == "system":
                continue
            projected.append(self._model_message_to_session_message(message))
        return tuple(projected)

    @staticmethod
    def _model_message_to_session_message(message: ModelMessage) -> dict[str, object]:
        if message.role == "user":
            return {"role": "user", "content": to_primitive(message.content)}
        if message.role == "assistant":
            content = message.content
            if not isinstance(content, dict):
                return {"role": "assistant", "content": to_primitive(content)}
            visible = content.get("content")
            completion = content.get("completion")
            if not visible and isinstance(completion, dict):
                visible = completion.get("summary")
            raw_calls = content.get("tool_calls") or []
            tool_calls: list[dict[str, object]] = []
            for raw_call in raw_calls:
                primitive_call = to_primitive(raw_call)
                if not isinstance(primitive_call, dict):
                    continue
                name = primitive_call.get("name")
                call_id = primitive_call.get("call_id")
                arguments = primitive_call.get("arguments")
                if not isinstance(name, str) or not isinstance(call_id, str):
                    continue
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(
                                arguments if isinstance(arguments, dict) else {},
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        },
                    }
                )
            result: dict[str, object] = {
                "role": "assistant",
                "content": to_primitive(visible if visible is not None else ""),
            }
            if tool_calls:
                result["tool_calls"] = tool_calls
            return result
        if message.role == "tool":
            content = message.content
            if isinstance(content, dict):
                content = content.get("content", "")
            result = {"role": "tool", "content": to_primitive(content)}
            if message.tool_call_id:
                result["tool_call_id"] = message.tool_call_id
            return result
        raise ValueError("employee run contains an unsupported message role")
