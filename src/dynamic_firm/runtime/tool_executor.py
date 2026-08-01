from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
from typing import Any, Mapping

from .company_coordination import CompanyCoordinationError, RemoteCompanyCoordinationClient
from .interruption import EffectInterruptionReason
from .models import (
    ActionPolicy,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalResumeState,
    ToolCall,
    ToolEffect,
    ToolRisk,
    ToolResult,
    Usage,
)
from .ports import ApprovalPort, CancellationToken, OperationCancelled
from .redaction import redact_prompt_text, redact_tool_output
from .store import ApprovalConflict, RunStore
from .tool_contracts import (
    PolicyDenied,
    ToolDefinition,
    ToolEffectNotStarted,
    ToolExecutionError,
    ToolRegistry,
    ToolValidationError,
)

class ToolExecutor:
    """Prepare, execute and commit a single explicitly granted tool action."""

    def __init__(
        self,
        registry: ToolRegistry,
        store: RunStore,
        *,
        approval_port: ApprovalPort | None = None,
        company_coordination: RemoteCompanyCoordinationClient | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.approval_port = approval_port
        self.company_coordination = company_coordination

    @staticmethod
    def action_id(run_id: str, model_call_index: int, tool_call_id: str) -> str:
        raw = f"{run_id}:{model_call_index}:{tool_call_id}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    async def execute(
        self,
        *,
        run_id: str,
        model_call_index: int,
        call: ToolCall,
        policy: ActionPolicy,
        cancellation: CancellationToken,
        prior_tool_calls: int,
        max_result_bytes: int,
        max_tool_output_bytes: int,
        current_usage: Usage,
        remaining_wall_ms: int,
        reserved_output_limit_bytes: int | None = None,
    ) -> ToolResult:
        action_id = self.action_id(run_id, model_call_index, call.call_id)
        arguments_json = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        arguments_hash = hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()
        definition = self.registry.get(call.name)
        validated: Mapping[str, Any] = call.arguments
        resource_key = f"unknown:{call.name}"
        validation_error: str | None = None
        if definition:
            try:
                validated = definition.validator(call.arguments)
                resource_key = definition.resource_key(validated)
            except (KeyError, TypeError, ValueError, ToolValidationError) as exc:
                validation_error = str(exc)

        action, created = self.store.record_tool_intent(
            run_id,
            action_id,
            model_call_index,
            call,
            arguments_hash,
            resource_key,
            effect=None if definition is None else definition.effect,
            idempotency_mode=(
                None if definition is None else definition.idempotency_mode.value
            ),
            usage_delta=Usage(tool_calls=1),
            new_usage=current_usage.plus(Usage(tool_calls=1)),
        )
        if not created:
            if action["tool_name"] != call.name or action["arguments_hash"] != arguments_hash:
                raise ToolExecutionError(
                    f"Action key {action_id} was reused with a different tool call"
                )
            replay = self.store.get_tool_result(action_id)
            if replay:
                self.store.complete_approval_resume(action_id)
                return replay
            approval = self.store.get_approval(action_id)
            if str(action["status"]) != "INTENT_RECORDED" or approval is None:
                raise ToolExecutionError(
                    f"Action {action_id} has no terminal result and cannot be resumed safely"
                )
            if approval.resume_state in {
                ApprovalResumeState.CLAIMED,
                ApprovalResumeState.COMPLETED,
            }:
                raise ToolExecutionError(
                    f"Action {action_id} resume was already claimed and is indeterminate"
                )

        if definition is None:
            result = ToolResult(call.call_id, call.name, False, "Unknown tool", action_id, "UNKNOWN_TOOL")
            self.store.mark_tool_terminal(action_id, result)
            raise PolicyDenied(f"Tool is not registered: {call.name}")
        if validation_error:
            result = ToolResult(
                call.call_id,
                call.name,
                False,
                redact_prompt_text(f"Invalid tool arguments: {validation_error}"),
                action_id,
                "INVALID_ARGUMENTS",
            )
            self.store.mark_tool_terminal(action_id, result)
            return result

        grant = next((item for item in policy.tool_grants if item.tool_name == definition.name), None)
        denial = self._policy_denial(
            definition,
            grant,
            policy,
            resource_key,
            prior_tool_calls,
        )
        if denial:
            denial = redact_prompt_text(denial)
            result = ToolResult(call.call_id, call.name, False, denial, action_id, "POLICY_DENIED")
            self.store.mark_tool_terminal(action_id, result)
            raise PolicyDenied(denial)

        approval_granted = action_id in policy.approval_grants
        needs_approval = (
            definition.requires_approval or bool(grant.requires_approval)
        ) and not policy.auto_approves(definition.name)
        if needs_approval and not approval_granted:
            existing_approval = self.store.get_approval(action_id)
            if self.approval_port is None and existing_approval is None:
                result = ToolResult(
                    call.call_id,
                    call.name,
                    False,
                    "Interactive approval is unavailable for this action",
                    action_id,
                    "APPROVAL_UNAVAILABLE",
                )
                self.store.mark_tool_terminal(action_id, result)
                return result
            run = self.store.get_run(run_id)
            if not run:
                raise ToolExecutionError(f"Run disappeared before approval: {run_id}")
            preview = (
                definition.approval_preview(validated)
                if definition.approval_preview
                else f"{definition.name} on {resource_key}"
            )
            preview = redact_prompt_text(preview)
            approval_resource_key = redact_prompt_text(resource_key)
            approval_request = ApprovalRequest(
                action_id=action_id,
                run_id=run_id,
                job_id=str(run["job_id"]),
                task_id=str(run["task_id"]),
                employee_id=str(run["employee_id"]),
                tool_name=definition.name,
                effect=definition.effect,
                risk=definition.risk,
                resource_key=approval_resource_key,
                preview=preview,
                allow_session=definition.allow_session_approval,
            )
            approval, _ = self.store.record_approval_request(approval_request)
            decision = approval.decision
            if decision is None:
                if self.approval_port is None:
                    raise ToolExecutionError(
                        f"Approval {action_id} is pending an external user decision"
                    )
                try:
                    decision = await self.approval_port.request(
                        approval_request,
                        cancellation,
                    )
                except OperationCancelled:
                    raise
                except Exception:
                    # An approval surface fault is not user intent.  Resolve
                    # the durable checkpoint explicitly so the action can be
                    # terminalized without leaving a phantom pending approval.
                    decision = ApprovalDecision.UNAVAILABLE
                if not isinstance(decision, ApprovalDecision):
                    decision = ApprovalDecision.UNAVAILABLE
                try:
                    approval = self.store.resolve_approval(
                        action_id,
                        decision,
                        decided_by="interactive-user",
                    ).approval
                except ApprovalConflict as exc:
                    raise ToolExecutionError(str(exc)) from exc
                decision = approval.decision
            if decision is None:
                raise ToolExecutionError(f"Approval {action_id} has no durable decision")
            if not self.store.claim_approval_resume(action_id):
                replay = self.store.get_tool_result(action_id)
                if replay:
                    self.store.complete_approval_resume(action_id)
                    return replay
                raise ToolExecutionError(
                    f"Approval {action_id} resume was already claimed and is indeterminate"
                )
            if decision not in {
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.ALLOW_SESSION,
            }:
                error_code = (
                    "APPROVAL_UNAVAILABLE"
                    if decision == ApprovalDecision.UNAVAILABLE
                    else "APPROVAL_DENIED"
                )
                message = (
                    "The approval interface was unavailable; this action was not approved"
                    if decision == ApprovalDecision.UNAVAILABLE
                    else "The user rejected this action"
                )
                result = ToolResult(
                    call.call_id,
                    call.name,
                    False,
                    message,
                    action_id,
                    error_code,
                )
                self.store.mark_tool_terminal(action_id, result)
                self.store.complete_approval_resume(action_id)
                return result

        cancellation.raise_if_cancelled()
        remote_resource: tuple[str, str, str, str] | None = None
        effectful = definition.effect in {
            ToolEffect.WRITE,
            ToolEffect.EXECUTE,
            ToolEffect.EXTERNAL_COMMUNICATION,
        }
        if effectful:
            if self.company_coordination is not None:
                run = self.store.get_run(run_id)
                job_id = str(run["job_id"]) if run is not None else ""
                resource_digest = hashlib.sha256(
                    f"noruct.effect-resource.v1|{resource_key}".encode("utf-8")
                ).hexdigest()
                lease_id = f"coord-lease-{action_id}"
                self.store.prepare_remote_effect_resource_claim(
                    action_id,
                    authority_digest=self.company_coordination.authority_digest,
                    origin=self.company_coordination.origin,
                    company_scope_digest=(
                        self.company_coordination.config.company_scope_digest
                    ),
                    device_id=self.company_coordination.config.device_id,
                    resource_digest=resource_digest,
                    lease_id=lease_id,
                )
                try:
                    remote_claim = await asyncio.to_thread(
                        self.company_coordination.claim_resource_lease,
                        job_id=job_id,
                        resource_digest=resource_digest,
                        lease_id=lease_id,
                    )
                except CompanyCoordinationError:
                    remote_claim = "UNAVAILABLE"
                if remote_claim == "UNAVAILABLE":
                    result = ToolResult(call.call_id, call.name, False, "Remote Company coordination is unavailable; this effectful action was not started.", action_id, "REMOTE_COORDINATION_UNAVAILABLE")
                    self.store.mark_tool_terminal(action_id, result)
                    self.store.complete_approval_resume(action_id)
                    return result
                if remote_claim is None:
                    result = ToolResult(call.call_id, call.name, False, "Another device owns this effectful resource; retry after its terminal receipt.", action_id, "REMOTE_RESOURCE_BUSY")
                    self.store.mark_tool_terminal(action_id, result)
                    self.store.complete_approval_resume(action_id)
                    return result
                remote_resource = (action_id, job_id, resource_digest, lease_id)
            if not self.store.acquire_effect_resource_lease(
                action_id=action_id,
                run_id=run_id,
                effect=definition.effect,
                resource_key=resource_key,
            ):
                if remote_resource is not None:
                    await self._release_remote_resource(
                        *remote_resource,
                        release_reason="LOCAL_PRE_HANDLER_REJECTION",
                    )
                result = ToolResult(
                    call.call_id,
                    call.name,
                    False,
                    "Another live action owns this effectful resource; retry after its terminal receipt.",
                    action_id,
                    "RESOURCE_BUSY",
                )
                self.store.mark_tool_terminal(action_id, result)
                self.store.complete_approval_resume(action_id)
                return result
        self.store.mark_tool_started(action_id)
        try:
            output = await asyncio.wait_for(
                definition.handler(validated, cancellation),
                timeout=max(1, min(definition.timeout_ms, remaining_wall_ms)) / 1000,
            )
            cancellation.raise_if_cancelled()
        except (OperationCancelled, asyncio.CancelledError):
            if effectful:
                self.store.mark_tool_effect_indeterminate(
                    action_id,
                    cause=EffectInterruptionReason.USER_CANCEL,
                )
                self.store.complete_approval_resume(action_id)
                raise
            result = ToolResult(call.call_id, call.name, False, "Tool cancelled", action_id, "CANCELLED")
            self.store.mark_tool_terminal(action_id, result)
            self.store.complete_approval_resume(action_id)
            if remote_resource is not None:
                await self._release_remote_resource(
                    *remote_resource,
                    release_reason="LOCAL_TERMINAL_RECEIPT",
                )
            raise
        except TimeoutError:
            if effectful:
                result = self.store.mark_tool_effect_indeterminate(
                    action_id,
                    cause=EffectInterruptionReason.DEADLINE_TIMEOUT,
                )
                self.store.complete_approval_resume(action_id)
                return result
            result = ToolResult(call.call_id, call.name, False, "Tool timed out", action_id, "TOOL_TIMEOUT")
            self.store.mark_tool_terminal(action_id, result)
            self.store.complete_approval_resume(action_id)
            if remote_resource is not None:
                await self._release_remote_resource(
                    *remote_resource,
                    release_reason="LOCAL_TERMINAL_RECEIPT",
                )
            return result
        except ToolEffectNotStarted:
            # This narrow trusted-tool contract is the only handler-entered
            # rejection that proves no observable effect began. Persist that
            # proof distinctly so a stranded remote owner remains explicitly
            # recoverable after a lost release response.
            result = ToolResult(
                call.call_id,
                call.name,
                False,
                "Tool rejected before its external effect began",
                action_id,
                "TOOL_REJECTED_BEFORE_EFFECT",
            )
            self.store.mark_tool_terminal(action_id, result)
            self.store.complete_approval_resume(action_id)
            if remote_resource is not None:
                await self._release_remote_resource(
                    *remote_resource,
                    release_reason="LOCAL_HANDLER_PROVED_NO_EFFECT",
                )
            return result
        except ToolValidationError:
            if effectful:
                result = self.store.mark_tool_effect_indeterminate(
                    action_id,
                    cause=EffectInterruptionReason.HANDLER_ERROR,
                )
                self.store.complete_approval_resume(action_id)
                return result
            # Tell the model a parent-owned tool was refused by a bounded
            # path/output rule, but do not forward model-controlled path text.
            result = ToolResult(
                call.call_id,
                call.name,
                False,
                "Tool rejected by its bounded path or output policy",
                action_id,
                "TOOL_REJECTED",
            )
            self.store.mark_tool_terminal(action_id, result)
            self.store.complete_approval_resume(action_id)
            if remote_resource is not None:
                await self._release_remote_resource(
                    *remote_resource,
                    release_reason="LOCAL_TERMINAL_RECEIPT",
                )
            return result
        except Exception as exc:
            if effectful:
                result = self.store.mark_tool_effect_indeterminate(
                    action_id,
                    cause=EffectInterruptionReason.HANDLER_ERROR,
                )
                self.store.complete_approval_resume(action_id)
                return result
            result = ToolResult(
                call.call_id,
                call.name,
                False,
                f"Tool failed: {type(exc).__name__}",
                action_id,
                "TOOL_ERROR",
            )
            self.store.mark_tool_terminal(action_id, result)
            self.store.complete_approval_resume(action_id)
            if remote_resource is not None:
                await self._release_remote_resource(
                    *remote_resource,
                    release_reason="LOCAL_TERMINAL_RECEIPT",
                )
            return result

        try:
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False, sort_keys=True)
            output = redact_tool_output(definition.name, validated, output)
        except Exception:
            if not effectful:
                raise
            # Returning from the handler is its success boundary.  A local
            # rendering fault must not tell the model to repeat the effect.
            output = "Effect completed; its result could not be rendered. Do not repeat it."
        remaining_tool_output = max(
            0,
            max_tool_output_bytes - self.store.get_tool_output_bytes(run_id),
        )
        output_limit = min(
            definition.output_limit_bytes,
            max_result_bytes,
            remaining_tool_output,
            (
                reserved_output_limit_bytes
                if reserved_output_limit_bytes is not None
                else remaining_tool_output
            ),
        )
        if len(output.encode("utf-8")) > output_limit and effectful:
            result = ToolResult(
                call.call_id,
                call.name,
                True,
                "Effect completed; its oversized result was omitted. Do not repeat it.",
                action_id,
            )
        elif len(output.encode("utf-8")) > output_limit:
            result = ToolResult(
                call.call_id,
                call.name,
                False,
                f"Tool output exceeded {output_limit} bytes",
                action_id,
                "OUTPUT_LIMIT",
            )
        else:
            result = ToolResult(call.call_id, call.name, True, output, action_id)
        self.store.mark_tool_terminal(action_id, result)
        self.store.complete_approval_resume(action_id)
        if remote_resource is not None:
            await self._release_remote_resource(
                *remote_resource,
                release_reason="LOCAL_TERMINAL_RECEIPT",
            )
        return result

    async def _release_remote_resource(
        self,
        action_id: str,
        job_id: str,
        resource_digest: str,
        lease_id: str,
        *,
        release_reason: str,
    ) -> None:
        """Release one exact owner and durably retain a safe remote closure."""

        if self.company_coordination is None:
            return
        try:
            released = await asyncio.to_thread(
                self.company_coordination.release_resource_lease,
                job_id=job_id,
                resource_digest=resource_digest,
                lease_id=lease_id,
            )
        except CompanyCoordinationError:
            return
        self.store.record_remote_effect_resource_release(
            job_id=job_id,
            action_id=action_id,
            remote_status="RELEASED" if released else "MISSING",
            release_reason=release_reason,
        )

    @staticmethod
    def _policy_denial(
        definition: ToolDefinition,
        grant,
        policy: ActionPolicy,
        resource_key: str,
        prior_tool_calls: int,
    ) -> str | None:
        if grant is None:
            return f"No explicit grant for tool {definition.name}"
        if definition.effect not in grant.allowed_effects:
            return f"Effect {definition.effect} is not granted for {definition.name}"
        if resource_key.startswith("workspace:"):
            if definition.effect == ToolEffect.READ and policy.filesystem_policy not in {
                "READ_ONLY",
                "WORKSPACE_WRITE",
            }:
                return "Filesystem policy does not allow workspace reads"
            if definition.effect == ToolEffect.WRITE and policy.filesystem_policy != "WORKSPACE_WRITE":
                return "Filesystem policy does not allow workspace writes"
        if definition.effect == ToolEffect.EXECUTE:
            browser_control = definition.name in {
                "navigate_browser_tab",
                "click_browser_element",
                "type_browser_text",
                "capture_browser_screenshot",
            }
            valid_profile = policy.sandbox_profile in {"host-workspace-approved", "remote-workspace-approved"}
            if definition.name == "run_remote_workspace_program" and policy.sandbox_profile == "remote-and-browser-approved":
                valid_profile = True
            if definition.name == "run_container_workspace_program" and policy.sandbox_profile == "host-workspace-approved":
                valid_profile = True
            if definition.name == "computer_use" and policy.sandbox_profile in {"computer-use-approved", "computer-and-browser-approved"}:
                valid_profile = True
            if browser_control and policy.sandbox_profile in {"browser-control-approved", "remote-and-browser-approved"}:
                valid_profile = True
            if not valid_profile:
                return "Command execution is outside the active authority profile"
        if (
            definition.effect == ToolEffect.NETWORK
            and policy.network_policy != "EXTERNAL_READ_ONLY"
        ):
            return "Network access is outside the active authority profile"
        if definition.effect == ToolEffect.EXTERNAL_COMMUNICATION:
            return "External communication is not supported by the local Product Shell"
        if prior_tool_calls >= grant.max_calls:
            return f"Tool grant call limit exhausted for {definition.name}"
        if not any(fnmatch.fnmatchcase(resource_key, pattern) for pattern in grant.resource_patterns):
            return f"Resource is outside the grant for {definition.name}"
        if definition.risk in {ToolRisk.HIGH, ToolRisk.IRREVERSIBLE} and not (
            definition.requires_approval
            or grant.requires_approval
            or policy.auto_approves(definition.name)
        ):
            return f"Risk {definition.risk} requires interactive approval"
        return None
