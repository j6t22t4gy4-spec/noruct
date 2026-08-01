from __future__ import annotations

import asyncio
import hashlib
import unittest
from dataclasses import replace
from typing import Awaitable, Callable

from dynamic_firm.runtime.interruption import EffectRecoveryOutcome
from dynamic_firm.runtime.models import (
    ActionPolicy,
    EventType,
    IdempotencyMode,
    RunStatus,
    ToolCall,
    ToolEffect,
    ToolGrant,
    ToolRisk,
)
from dynamic_firm.runtime.ports import CancellationToken, OperationCancelled
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import (
    ToolDefinition,
    ToolExecutionError,
    ToolExecutor,
    ToolRegistry,
)
from tests.runtime.helpers import make_request


_EFFECTS = (
    ToolEffect.WRITE,
    ToolEffect.EXECUTE,
    ToolEffect.EXTERNAL_COMMUNICATION,
)
_EVIDENCE_DIGEST = hashlib.sha256(b"external-effect-recovery-evidence").hexdigest()


class _EffectContractExecutor(ToolExecutor):
    """Exercise the common effect contract below Product Shell policy.

    The Product Shell intentionally does not expose an external-communication
    tool yet.  This test-only executor bypasses only that presentation policy
    so all three effect classes share the same durable recovery regression.
    """

    @staticmethod
    def _policy_denial(
        definition,
        grant,
        policy,
        resource_key,
        prior_tool_calls,
    ):  # type: ignore[no-untyped-def]
        denial = ToolExecutor._policy_denial(
            definition,
            grant,
            policy,
            resource_key,
            prior_tool_calls,
        )
        if (
            definition.effect is ToolEffect.EXTERNAL_COMMUNICATION
            and denial == "External communication is not supported by the local Product Shell"
        ):
            return None
        return denial


class _OneShotTerminalReceiptFailureStore(RunStore):
    """Lose one post-handler terminal commit without losing the intent."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_terminal_receipt = True

    def mark_tool_terminal(self, action_id, result):  # type: ignore[no-untyped-def]
        if self.fail_next_terminal_receipt:
            self.fail_next_terminal_receipt = False
            raise RuntimeError("injected terminal receipt commit failure")
        return super().mark_tool_terminal(action_id, result)


def _tool_name(effect: ToolEffect) -> str:
    return f"effect_fixture_{effect.value.lower()}"


def _policy(effect: ToolEffect) -> ActionPolicy:
    name = _tool_name(effect)
    return ActionPolicy(
        tool_grants=(
            ToolGrant(
                tool_name=name,
                allowed_effects=(effect,),
                resource_patterns=("effect-recovery:*",),
                max_calls=8,
            ),
        ),
        filesystem_policy="WORKSPACE_WRITE",
        sandbox_profile="host-workspace-approved",
    )


def _definition(
    effect: ToolEffect,
    handler: Callable[[object, CancellationToken], Awaitable[str]],
    *,
    timeout_ms: int = 1_000,
) -> ToolDefinition:
    return ToolDefinition(
        name=_tool_name(effect),
        description="Exercise the durable effect recovery contract.",
        input_schema={"type": "object", "additionalProperties": False},
        effect=effect,
        risk=ToolRisk.LOW,
        idempotency_mode=IdempotencyMode.NONE,
        validator=lambda value: value,
        resource_key=lambda value: "effect-recovery:shared-resource",
        handler=handler,  # type: ignore[arg-type]
        timeout_ms=timeout_ms,
    )


def _stage_run(
    store: RunStore,
    *,
    effect: ToolEffect,
    request_id: str,
    job_id: str,
    task_id: str,
):
    base = make_request(request_id=request_id)
    request = replace(
        base,
        task=replace(base.task, job_id=job_id, task_id=task_id),
        action_policy=_policy(effect),
    )
    handle, created = store.create_run(request)
    if not created:
        raise AssertionError("effect recovery fixture request must be unique")
    store.begin_run(handle.run_id)
    return request, handle


async def _execute(
    executor: ToolExecutor,
    store: RunStore,
    request,
    handle,
    call: ToolCall,
    cancellation: CancellationToken | None = None,
    *,
    prior_tool_calls: int = 0,
):
    return await executor.execute(
        run_id=handle.run_id,
        model_call_index=1,
        call=call,
        policy=request.action_policy,
        cancellation=cancellation or CancellationToken(),
        prior_tool_calls=prior_tool_calls,
        max_result_bytes=request.limits.max_result_bytes,
        max_tool_output_bytes=request.limits.max_tool_output_bytes,
        current_usage=store.get_usage(handle.run_id),
        remaining_wall_ms=1_000,
    )


def _receipt(store: RunStore, job_id: str, action_id: str):
    return next(
        item
        for item in store.list_job_tool_receipts(job_id)
        if item["action_id"] == action_id
    )


class EffectRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_disruptions_seal_every_effect_class_without_replay(self) -> None:
        for effect in _EFFECTS:
            for disruption in ("timeout", "exception", "cancel"):
                with self.subTest(effect=effect.value, disruption=disruption):
                    store = RunStore()
                    calls: list[str] = []
                    entered = asyncio.Event()

                    async def handler(arguments, cancellation):  # type: ignore[no-untyped-def]
                        del arguments
                        calls.append("external effect occurred")
                        entered.set()
                        if disruption == "timeout":
                            await asyncio.sleep(60)
                        if disruption == "exception":
                            raise RuntimeError("failure after effect")
                        await cancellation.wait()
                        raise OperationCancelled(cancellation.reason)

                    registry = ToolRegistry()
                    registry.register(
                        _definition(
                            effect,
                            handler,
                            timeout_ms=5 if disruption == "timeout" else 1_000,
                        )
                    )
                    executor = _EffectContractExecutor(registry, store)
                    owner_request, owner = _stage_run(
                        store,
                        effect=effect,
                        request_id=f"effect-{effect.value}-{disruption}-owner",
                        job_id=f"job-{effect.value}-{disruption}-owner",
                        task_id="task-owner",
                    )
                    call = ToolCall("effect-call", _tool_name(effect), {})
                    action_id = executor.action_id(owner.run_id, 1, call.call_id)

                    if disruption == "cancel":
                        token = CancellationToken()
                        task = asyncio.create_task(
                            _execute(executor, store, owner_request, owner, call, token)
                        )
                        await asyncio.wait_for(entered.wait(), 1.0)
                        token.cancel("operator cancelled after handler entry")
                        with self.assertRaises(OperationCancelled):
                            await task
                    else:
                        result = await _execute(
                            executor,
                            store,
                            owner_request,
                            owner,
                            call,
                        )
                        self.assertFalse(result.ok)
                        self.assertEqual(result.error_code, "EFFECT_OUTCOME_UNKNOWN")

                    self.assertEqual(calls, ["external effect occurred"])
                    self.assertEqual(
                        _receipt(store, owner_request.task.job_id, action_id)["status"],
                        "INDETERMINATE",
                    )
                    unknown_events = [
                        event
                        for event in store.list_events(owner.run_id)
                        if event.type is EventType.TOOL_EFFECT_OUTCOME_UNKNOWN
                    ]
                    self.assertEqual(len(unknown_events), 1)
                    synthetic = store.get_tool_result(action_id)
                    self.assertIsNotNone(synthetic)
                    self.assertEqual(synthetic.error_code, "EFFECT_OUTCOME_UNKNOWN")  # type: ignore[union-attr]

                    replay = await _execute(
                        executor,
                        store,
                        owner_request,
                        owner,
                        call,
                        prior_tool_calls=1,
                    )
                    self.assertTrue(replay.replayed)
                    self.assertEqual(replay.error_code, "EFFECT_OUTCOME_UNKNOWN")
                    self.assertEqual(calls, ["external effect occurred"])

                    contender_request, contender = _stage_run(
                        store,
                        effect=effect,
                        request_id=f"effect-{effect.value}-{disruption}-contender",
                        job_id=f"job-{effect.value}-{disruption}-contender",
                        task_id="task-contender",
                    )
                    blocked = await _execute(
                        executor,
                        store,
                        contender_request,
                        contender,
                        ToolCall("contender-call", _tool_name(effect), {}),
                    )
                    self.assertFalse(blocked.ok)
                    self.assertEqual(blocked.error_code, "RESOURCE_BUSY")
                    self.assertEqual(calls, ["external effect occurred"])
                    store.close()

    async def test_process_recovery_seals_effect_after_terminal_receipt_failure(self) -> None:
        for effect in _EFFECTS:
            with self.subTest(effect=effect.value):
                store = _OneShotTerminalReceiptFailureStore()
                calls: list[str] = []

                async def handler(arguments, cancellation):  # type: ignore[no-untyped-def]
                    del arguments, cancellation
                    calls.append("external effect occurred")
                    return "handler returned after effect"

                registry = ToolRegistry()
                registry.register(_definition(effect, handler))
                executor = _EffectContractExecutor(registry, store)
                request, handle = _stage_run(
                    store,
                    effect=effect,
                    request_id=f"receipt-failure-{effect.value}",
                    job_id=f"job-receipt-failure-{effect.value}",
                    task_id="task-owner",
                )
                call = ToolCall("effect-call", _tool_name(effect), {})
                action_id = executor.action_id(handle.run_id, 1, call.call_id)

                with self.assertRaisesRegex(RuntimeError, "terminal receipt commit failure"):
                    await _execute(executor, store, request, handle, call)
                self.assertEqual(calls, ["external effect occurred"])
                self.assertEqual(
                    _receipt(store, request.task.job_id, action_id)["status"],
                    "STARTED",
                )
                with self.assertRaises(ToolExecutionError):
                    await _execute(
                        executor,
                        store,
                        request,
                        handle,
                        call,
                        prior_tool_calls=1,
                    )
                self.assertEqual(calls, ["external effect occurred"])

                recovered = store.recover_interrupted_runs()
                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].status, RunStatus.FAILED)
                self.assertEqual(store.get_status(handle.run_id), RunStatus.FAILED)
                self.assertEqual(
                    _receipt(store, request.task.job_id, action_id)["status"],
                    "INDETERMINATE",
                )
                self.assertEqual(
                    sum(
                        event.type is EventType.TOOL_EFFECT_OUTCOME_UNKNOWN
                        for event in store.list_events(handle.run_id)
                    ),
                    1,
                )

                replay = await _execute(
                    executor,
                    store,
                    request,
                    handle,
                    call,
                    prior_tool_calls=1,
                )
                self.assertTrue(replay.replayed)
                self.assertEqual(replay.error_code, "EFFECT_OUTCOME_UNKNOWN")
                self.assertEqual(calls, ["external effect occurred"])

                contender_request, contender = _stage_run(
                    store,
                    effect=effect,
                    request_id=f"receipt-failure-{effect.value}-contender",
                    job_id=f"job-receipt-failure-{effect.value}-contender",
                    task_id="task-contender",
                )
                blocked = await _execute(
                    executor,
                    store,
                    contender_request,
                    contender,
                    ToolCall("contender-call", _tool_name(effect), {}),
                )
                self.assertEqual(blocked.error_code, "RESOURCE_BUSY")
                self.assertEqual(calls, ["external effect occurred"])
                store.close()

    async def test_evidenced_no_effect_and_compensation_release_a_terminal_case(self) -> None:
        scenarios = (
            (ToolEffect.WRITE, EffectRecoveryOutcome.CONFIRMED_NO_EFFECT),
            (ToolEffect.EXECUTE, EffectRecoveryOutcome.COMPENSATED),
        )
        for effect, outcome in scenarios:
            with self.subTest(effect=effect.value, outcome=outcome.value):
                store = RunStore()
                calls = 0

                async def handler(arguments, cancellation):  # type: ignore[no-untyped-def]
                    del arguments, cancellation
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise RuntimeError("outcome unknown after effect boundary")
                    return "new action completed"

                registry = ToolRegistry()
                registry.register(_definition(effect, handler))
                executor = _EffectContractExecutor(registry, store)
                request, handle = _stage_run(
                    store,
                    effect=effect,
                    request_id=f"resolution-{outcome.value}-owner",
                    job_id=f"job-resolution-{outcome.value}",
                    task_id="task-owner",
                )
                call = ToolCall("effect-call", _tool_name(effect), {})
                action_id = executor.action_id(handle.run_id, 1, call.call_id)
                unknown = await _execute(executor, store, request, handle, call)
                self.assertEqual(unknown.error_code, "EFFECT_OUTCOME_UNKNOWN")

                with self.assertRaisesRegex(ValueError, "active run"):
                    store.resolve_effect_recovery_case(
                        job_id=request.task.job_id,
                        action_id=action_id,
                        outcome=outcome,
                        evidence_digest=_EVIDENCE_DIGEST,
                        resolved_by="operator-fixture",
                        reason="verified external recovery evidence",
                    )
                store.recover_interrupted_runs()
                with self.assertRaisesRegex(ValueError, "requires evidence"):
                    store.resolve_effect_recovery_case(
                        job_id=request.task.job_id,
                        action_id=action_id,
                        outcome=outcome,
                        evidence_digest=None,
                        resolved_by="operator-fixture",
                        reason="verified external recovery evidence",
                    )

                resolution = store.resolve_effect_recovery_case(
                    job_id=request.task.job_id,
                    action_id=action_id,
                    outcome=outcome,
                    evidence_digest=_EVIDENCE_DIGEST,
                    resolved_by="operator-fixture",
                    reason="verified external recovery evidence",
                )
                repeated = store.resolve_effect_recovery_case(
                    job_id=request.task.job_id,
                    action_id=action_id,
                    outcome=outcome,
                    evidence_digest=_EVIDENCE_DIGEST,
                    resolved_by="operator-fixture",
                    reason="verified external recovery evidence",
                )
                self.assertTrue(resolution["resource_released"])
                self.assertEqual(repeated, resolution)
                case = store.list_job_effect_recovery_cases(request.task.job_id)[0]
                self.assertEqual(case["case_status"], "RESOLVED")
                self.assertEqual(case["evidence_digest"], _EVIDENCE_DIGEST)
                self.assertFalse(case["lease_held"])

                with self.assertRaisesRegex(ValueError, "different resolution"):
                    store.resolve_effect_recovery_case(
                        job_id=request.task.job_id,
                        action_id=action_id,
                        outcome=EffectRecoveryOutcome.SEALED_UNKNOWN,
                        evidence_digest=_EVIDENCE_DIGEST,
                        resolved_by="different-operator",
                        reason="conflicting conclusion",
                    )

                contender_request, contender = _stage_run(
                    store,
                    effect=effect,
                    request_id=f"resolution-{outcome.value}-contender",
                    job_id=f"job-resolution-{outcome.value}-contender",
                    task_id="task-contender",
                )
                allowed = await _execute(
                    executor,
                    store,
                    contender_request,
                    contender,
                    ToolCall("contender-call", _tool_name(effect), {}),
                )
                self.assertTrue(allowed.ok)
                self.assertEqual(calls, 2)
                store.close()

    async def test_sealed_unknown_resolution_never_releases_the_resource(self) -> None:
        store = RunStore()
        calls = 0

        async def handler(arguments, cancellation):  # type: ignore[no-untyped-def]
            del arguments, cancellation
            nonlocal calls
            calls += 1
            raise RuntimeError("external outcome cannot be proved")

        effect = ToolEffect.EXTERNAL_COMMUNICATION
        registry = ToolRegistry()
        registry.register(_definition(effect, handler))
        executor = _EffectContractExecutor(registry, store)
        request, handle = _stage_run(
            store,
            effect=effect,
            request_id="sealed-unknown-owner",
            job_id="job-sealed-unknown-owner",
            task_id="task-owner",
        )
        call = ToolCall("effect-call", _tool_name(effect), {})
        action_id = executor.action_id(handle.run_id, 1, call.call_id)
        result = await _execute(executor, store, request, handle, call)
        self.assertEqual(result.error_code, "EFFECT_OUTCOME_UNKNOWN")
        store.recover_interrupted_runs()

        resolution = store.resolve_effect_recovery_case(
            job_id=request.task.job_id,
            action_id=action_id,
            outcome=EffectRecoveryOutcome.SEALED_UNKNOWN,
            evidence_digest=None,
            resolved_by="operator-fixture",
            reason="no trustworthy external evidence exists",
        )
        self.assertFalse(resolution["resource_released"])
        case = store.list_job_effect_recovery_cases(request.task.job_id)[0]
        self.assertEqual(case["case_status"], "SEALED_UNKNOWN")
        self.assertTrue(case["lease_held"])

        contender_request, contender = _stage_run(
            store,
            effect=effect,
            request_id="sealed-unknown-contender",
            job_id="job-sealed-unknown-contender",
            task_id="task-contender",
        )
        blocked = await _execute(
            executor,
            store,
            contender_request,
            contender,
            ToolCall("contender-call", _tool_name(effect), {}),
        )
        self.assertEqual(blocked.error_code, "RESOURCE_BUSY")
        self.assertEqual(calls, 1)

        with self.assertRaisesRegex(ValueError, "different resolution"):
            store.resolve_effect_recovery_case(
                job_id=request.task.job_id,
                action_id=action_id,
                outcome=EffectRecoveryOutcome.COMPENSATED,
                evidence_digest=_EVIDENCE_DIGEST,
                resolved_by="operator-fixture",
                reason="late conflicting claim",
            )
        store.close()


if __name__ == "__main__":
    unittest.main()
