from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.runtime.models import (
    ActionPolicy,
    EventType,
    IdempotencyMode,
    ModelResponse,
    RunStatus,
    ToolCall,
    ToolEffect,
    ToolGrant,
    ToolRisk,
    Usage,
)
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.company_coordination import CompanyCoordinationError
from dynamic_firm.runtime.interruption import EffectRecoveryOutcome
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import (
    FixtureReader,
    ToolDefinition,
    ToolEffectNotStarted,
    ToolExecutionError,
    ToolExecutor,
    ToolRegistry,
)
from tests.runtime.helpers import completion, make_request


def _with_remote_identity(coordination, *, device_id="device-test-a"):  # type: ignore[no-untyped-def]
    coordination.config = SimpleNamespace(
        company_scope_digest="d" * 64,
        device_id=device_id,
    )
    coordination.origin = "https://coordination.example.test"
    coordination.authority_digest = "e" * 64
    return coordination


class RuntimeIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_pre_effect_rejection_is_terminal_and_remotely_recoverable(self) -> None:
        class LostReleaseCoordination:
            def claim_resource_lease(self, **value):  # type: ignore[no-untyped-def]
                del value
                return object()

            def release_resource_lease(self, **value):  # type: ignore[no-untyped-def]
                del value
                raise CompanyCoordinationError("fixture release response lost")

        async def handler(arguments, cancellation):  # type: ignore[no-untyped-def]
            del arguments, cancellation
            raise ToolEffectNotStarted("fixture rejected before write")

        store = RunStore()
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="write_fixture",
                description="Write fixture",
                input_schema={"type": "object"},
                effect=ToolEffect.WRITE,
                risk=ToolRisk.MEDIUM,
                idempotency_mode=IdempotencyMode.CALL_KEY,
                validator=lambda value: value,
                resource_key=lambda value: "fixture:typed-pre-effect",
                handler=handler,
            )
        )
        policy = ActionPolicy(
            tool_grants=(
                ToolGrant(
                    "write_fixture",
                    (ToolEffect.WRITE,),
                    ("fixture:*",),
                    1,
                ),
            ),
            filesystem_policy="WORKSPACE_WRITE",
        )
        request = replace(
            make_request(request_id="typed-pre-effect"),
            action_policy=policy,
        )
        handle, _ = store.create_run(request)
        store.begin_run(handle.run_id)
        result = await ToolExecutor(
            registry,
            store,
            company_coordination=_with_remote_identity(  # type: ignore[arg-type]
                LostReleaseCoordination()
            ),
        ).execute(
            run_id=handle.run_id,
            model_call_index=1,
            call=ToolCall("typed-pre-effect", "write_fixture", {}),
            policy=policy,
            cancellation=CancellationToken(),
            prior_tool_calls=0,
            max_result_bytes=request.limits.max_result_bytes,
            max_tool_output_bytes=request.limits.max_tool_output_bytes,
            current_usage=store.get_usage(handle.run_id),
            remaining_wall_ms=1_000,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TOOL_REJECTED_BEFORE_EFFECT")
        self.assertEqual(
            store.list_job_effect_recovery_cases(request.task.job_id),
            (),
        )
        store.recover_interrupted_runs()
        projection = store.list_job_remote_effect_resource_claims(
            request.task.job_id
        )
        self.assertEqual(len(projection), 1)
        self.assertEqual(
            projection[0]["next_action"],
            "CONFIRM_NO_EFFECT_AND_RELEASE_EXACT_OWNER",
        )
        self.assertEqual(projection[0]["case_status"], "OPEN")
        action_id = ToolExecutor.action_id(
            handle.run_id,
            1,
            "typed-pre-effect",
        )
        evidence_digest = "f" * 64
        store.record_remote_effect_resource_release(
            job_id=request.task.job_id,
            action_id=action_id,
            remote_status="MISSING",
            release_reason="OPERATOR_EFFECT_RESOLUTION",
        )
        store.validate_terminal_remote_effect_resolution(
            job_id=request.task.job_id,
            action_id=action_id,
            outcome=EffectRecoveryOutcome.CONFIRMED_NO_EFFECT,
            evidence_digest=evidence_digest,
            resolved_by="operator-test",
            reason="typed pre-effect receipt and exact remote closure reviewed",
        )
        resolved = store.resolve_terminal_remote_effect_resource(
            job_id=request.task.job_id,
            action_id=action_id,
            outcome=EffectRecoveryOutcome.CONFIRMED_NO_EFFECT,
            evidence_digest=evidence_digest,
            resolved_by="operator-test",
            reason="typed pre-effect receipt and exact remote closure reviewed",
        )
        self.assertTrue(resolved["resource_released"])
        store.close()

    async def test_remote_company_coordination_blocks_same_effect_across_local_stores(self) -> None:
        """Two device-local stores must still serialize one effectful resource."""

        class SharedCoordination:
            def __init__(self) -> None:
                self.leases: dict[str, str] = {}

            def claim_resource_lease(self, *, job_id, resource_digest, lease_id, ttl_seconds=300):  # type: ignore[no-untyped-def]
                del job_id, ttl_seconds
                current = self.leases.get(resource_digest)
                if current is not None and current != lease_id:
                    return None
                self.leases[resource_digest] = lease_id
                return object()

            def release_resource_lease(self, *, job_id, resource_digest, lease_id):  # type: ignore[no-untyped-def]
                del job_id
                if self.leases.get(resource_digest) == lease_id:
                    del self.leases[resource_digest]
                    return True
                return False

        coordination = _with_remote_identity(SharedCoordination())
        first_store, second_store = RunStore(), RunStore()
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def handler(arguments, cancellation):  # type: ignore[no-untyped-def]
            del arguments, cancellation
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "written"

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="write_fixture", description="Write fixture", input_schema={"type": "object"},
            effect=ToolEffect.WRITE, risk=ToolRisk.MEDIUM, idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=lambda value: value, resource_key=lambda value: "fixture:cross-device", handler=handler,
        ))
        policy = ActionPolicy(tool_grants=(ToolGrant("write_fixture", (ToolEffect.WRITE,), ("fixture:*",), 2),), filesystem_policy="WORKSPACE_WRITE")
        first_request = replace(make_request(request_id="remote-owner"), action_policy=policy)
        second_request = replace(make_request(request_id="remote-contender"), task=replace(make_request().task, job_id="job-22222222", task_id="task-2"), action_policy=policy)
        first_handle, _ = first_store.create_run(first_request)
        second_handle, _ = second_store.create_run(second_request)
        first_store.begin_run(first_handle.run_id)
        second_store.begin_run(second_handle.run_id)
        first_executor = ToolExecutor(registry, first_store, company_coordination=coordination)  # type: ignore[arg-type]
        second_executor = ToolExecutor(registry, second_store, company_coordination=coordination)  # type: ignore[arg-type]

        def execute(executor, store, handle, request, call):  # type: ignore[no-untyped-def]
            return executor.execute(
                run_id=handle.run_id, model_call_index=1, call=call, policy=policy,
                cancellation=CancellationToken(), prior_tool_calls=0,
                max_result_bytes=request.limits.max_result_bytes,
                max_tool_output_bytes=request.limits.max_tool_output_bytes,
                current_usage=store.get_usage(handle.run_id), remaining_wall_ms=1_000,
            )

        owner_task = asyncio.create_task(execute(first_executor, first_store, first_handle, first_request, ToolCall("owner", "write_fixture", {})))
        await started.wait()
        busy = await execute(second_executor, second_store, second_handle, second_request, ToolCall("contender", "write_fixture", {}))
        self.assertFalse(busy.ok)
        self.assertEqual(busy.error_code, "REMOTE_RESOURCE_BUSY")
        release.set()
        owner = await owner_task
        self.assertTrue(owner.ok)
        retry = await execute(second_executor, second_store, second_handle, second_request, ToolCall("retry", "write_fixture", {}))
        self.assertTrue(retry.ok)
        self.assertEqual(calls, 2)
        first_store.close()
        second_store.close()

    async def test_remote_lease_survives_indeterminate_terminal_run_and_unknown_seal(self) -> None:
        """Neither terminalization nor SEALED_UNKNOWN releases remote ownership."""

        class SharedCoordination:
            def __init__(self) -> None:
                self.leases: dict[str, str] = {}
                self.releases: list[str] = []

            def claim_resource_lease(self, *, job_id, resource_digest, lease_id, ttl_seconds=300):  # type: ignore[no-untyped-def]
                del job_id, ttl_seconds
                current = self.leases.get(resource_digest)
                if current is not None and current != lease_id:
                    return None
                self.leases[resource_digest] = lease_id
                return object()

            def release_resource_lease(self, *, job_id, resource_digest, lease_id):  # type: ignore[no-untyped-def]
                del job_id
                self.releases.append(lease_id)
                if self.leases.get(resource_digest) == lease_id:
                    del self.leases[resource_digest]
                    return True
                return False

        coordination = _with_remote_identity(SharedCoordination())
        owner_store, contender_store = RunStore(), RunStore()
        handler_calls = 0

        async def handler(arguments, cancellation):  # type: ignore[no-untyped-def]
            del arguments, cancellation
            nonlocal handler_calls
            handler_calls += 1
            raise RuntimeError("external outcome is unknown")

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="write_fixture",
                description="Write fixture",
                input_schema={"type": "object"},
                effect=ToolEffect.WRITE,
                risk=ToolRisk.MEDIUM,
                idempotency_mode=IdempotencyMode.CALL_KEY,
                validator=lambda value: value,
                resource_key=lambda value: "fixture:remote-indeterminate",
                handler=handler,
            )
        )
        policy = ActionPolicy(
            tool_grants=(
                ToolGrant(
                    "write_fixture",
                    (ToolEffect.WRITE,),
                    ("fixture:*",),
                    2,
                ),
            ),
            filesystem_policy="WORKSPACE_WRITE",
        )
        owner_request = replace(
            make_request(request_id="remote-indeterminate-owner"),
            action_policy=policy,
        )
        contender_request = replace(
            make_request(request_id="remote-indeterminate-contender"),
            task=replace(
                make_request().task,
                job_id="job-22222222",
                task_id="task-2",
            ),
            action_policy=policy,
        )
        owner_handle, _ = owner_store.create_run(owner_request)
        contender_handle, _ = contender_store.create_run(contender_request)
        owner_store.begin_run(owner_handle.run_id)
        contender_store.begin_run(contender_handle.run_id)

        async def execute(store, handle, request, call_id):  # type: ignore[no-untyped-def]
            return await ToolExecutor(
                registry,
                store,
                company_coordination=coordination,  # type: ignore[arg-type]
            ).execute(
                run_id=handle.run_id,
                model_call_index=1,
                call=ToolCall(call_id, "write_fixture", {}),
                policy=policy,
                cancellation=CancellationToken(),
                prior_tool_calls=0,
                max_result_bytes=request.limits.max_result_bytes,
                max_tool_output_bytes=request.limits.max_tool_output_bytes,
                current_usage=store.get_usage(handle.run_id),
                remaining_wall_ms=1_000,
            )

        unknown = await execute(
            owner_store,
            owner_handle,
            owner_request,
            "owner-effect",
        )
        self.assertEqual(unknown.error_code, "EFFECT_OUTCOME_UNKNOWN")
        self.assertEqual(coordination.releases, [])

        owner_store.recover_interrupted_runs()
        case = owner_store.list_job_effect_recovery_cases(
            owner_request.task.job_id
        )[0]
        owner_store.resolve_effect_recovery_case(
            job_id=owner_request.task.job_id,
            action_id=str(case["action_id"]),
            outcome=EffectRecoveryOutcome.SEALED_UNKNOWN,
            evidence_digest=None,
            resolved_by="operator-fixture",
            reason="no trustworthy external evidence exists",
        )
        self.assertEqual(coordination.releases, [])

        blocked = await execute(
            contender_store,
            contender_handle,
            contender_request,
            "contender-effect",
        )
        self.assertEqual(blocked.error_code, "REMOTE_RESOURCE_BUSY")
        self.assertEqual(handler_calls, 1)
        self.assertEqual(coordination.releases, [])
        owner_store.close()
        contender_store.close()

    async def test_remote_coordination_outage_never_starts_an_effect_handler(self) -> None:
        """A coordination outage fails closed before a local write starts."""

        class UnavailableCoordination:
            def claim_resource_lease(self, **value):  # type: ignore[no-untyped-def]
                del value
                raise CompanyCoordinationError("fixture transport outage")

        store = RunStore()
        calls = 0

        async def handler(arguments, cancellation):  # type: ignore[no-untyped-def]
            del arguments, cancellation
            nonlocal calls
            calls += 1
            return "must not run"

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="write_fixture", description="Write fixture", input_schema={"type": "object"},
            effect=ToolEffect.WRITE, risk=ToolRisk.MEDIUM, idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=lambda value: value, resource_key=lambda value: "fixture:remote-outage", handler=handler,
        ))
        policy = ActionPolicy(
            tool_grants=(ToolGrant("write_fixture", (ToolEffect.WRITE,), ("fixture:*",), 1),),
            filesystem_policy="WORKSPACE_WRITE",
        )
        request = replace(make_request(request_id="remote-outage"), action_policy=policy)
        handle, _ = store.create_run(request)
        store.begin_run(handle.run_id)
        unavailable = _with_remote_identity(UnavailableCoordination())
        result = await ToolExecutor(
            registry,
            store,
            company_coordination=unavailable,  # type: ignore[arg-type]
        ).execute(
            run_id=handle.run_id,
            model_call_index=1,
            call=ToolCall("remote-outage", "write_fixture", {}),
            policy=policy,
            cancellation=CancellationToken(),
            prior_tool_calls=0,
            max_result_bytes=request.limits.max_result_bytes,
            max_tool_output_bytes=request.limits.max_tool_output_bytes,
            current_usage=store.get_usage(handle.run_id),
            remaining_wall_ms=1_000,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "REMOTE_COORDINATION_UNAVAILABLE")
        self.assertEqual(calls, 0)
        remote = store.remote_effect_resource_claim(
            job_id=request.task.job_id,
            action_id=ToolExecutor.action_id(
                handle.run_id,
                1,
                "remote-outage",
            ),
        )
        self.assertIsNotNone(remote)
        assert remote is not None
        self.assertFalse(remote["remote_closed"])
        self.assertNotIn("fixture:remote-outage", json.dumps(remote))
        store.close()

    async def test_local_lease_conflict_compensates_a_remote_claim_before_handler(self) -> None:
        """A remote claim is released if a same-device durable lease rejects it."""

        class RecordingCoordination:
            def __init__(self) -> None:
                self.claims = 0
                self.releases: list[tuple[str, str, str]] = []

            def claim_resource_lease(self, **value):  # type: ignore[no-untyped-def]
                self.claims += 1
                return object()

            def release_resource_lease(self, *, job_id, resource_digest, lease_id):  # type: ignore[no-untyped-def]
                self.releases.append((job_id, resource_digest, lease_id))
                return True

        store = RunStore()
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def handler(arguments, cancellation):  # type: ignore[no-untyped-def]
            del arguments, cancellation
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "written"

        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="write_fixture", description="Write fixture", input_schema={"type": "object"},
            effect=ToolEffect.WRITE, risk=ToolRisk.MEDIUM, idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=lambda value: value, resource_key=lambda value: "fixture:local-conflict", handler=handler,
        ))
        policy = ActionPolicy(
            tool_grants=(ToolGrant("write_fixture", (ToolEffect.WRITE,), ("fixture:*",), 1),),
            filesystem_policy="WORKSPACE_WRITE",
        )
        owner_request = replace(make_request(request_id="local-owner"), action_policy=policy)
        contender_request = replace(
            make_request(request_id="local-contender"),
            task=replace(make_request().task, job_id="job-22222222", task_id="task-2"),
            action_policy=policy,
        )
        owner_handle, _ = store.create_run(owner_request)
        contender_handle, _ = store.create_run(contender_request)
        store.begin_run(owner_handle.run_id)
        store.begin_run(contender_handle.run_id)
        owner = ToolExecutor(registry, store)
        coordination = _with_remote_identity(RecordingCoordination())
        contender = ToolExecutor(registry, store, company_coordination=coordination)  # type: ignore[arg-type]

        def execute(executor, handle, request, call):  # type: ignore[no-untyped-def]
            return executor.execute(
                run_id=handle.run_id, model_call_index=1, call=call, policy=policy,
                cancellation=CancellationToken(), prior_tool_calls=0,
                max_result_bytes=request.limits.max_result_bytes,
                max_tool_output_bytes=request.limits.max_tool_output_bytes,
                current_usage=store.get_usage(handle.run_id), remaining_wall_ms=1_000,
            )

        owner_task = asyncio.create_task(
            execute(owner, owner_handle, owner_request, ToolCall("owner", "write_fixture", {}))
        )
        await started.wait()
        blocked = await execute(
            contender, contender_handle, contender_request, ToolCall("contender", "write_fixture", {})
        )
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error_code, "RESOURCE_BUSY")
        self.assertEqual(calls, 1)
        self.assertEqual(coordination.claims, 1)
        self.assertEqual(len(coordination.releases), 1)

        release.set()
        self.assertTrue((await owner_task).ok)
        store.close()

    async def test_effectful_resource_lease_blocks_only_the_same_live_resource(self) -> None:
        store = RunStore()
        started = asyncio.Event()
        release = asyncio.Event()
        handler_calls = 0

        async def handler(arguments, cancellation):  # type: ignore[no-untyped-def]
            nonlocal handler_calls
            handler_calls += 1
            started.set()
            await release.wait()
            return "write receipt"

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="write_fixture",
                description="Write one fixture resource.",
                input_schema={"type": "object", "properties": {}},
                effect=ToolEffect.WRITE,
                risk=ToolRisk.MEDIUM,
                idempotency_mode=IdempotencyMode.CALL_KEY,
                validator=lambda value: value,
                resource_key=lambda value: "fixture:shared-effect",
                handler=handler,
            )
        )
        policy = ActionPolicy(
            tool_grants=(
                ToolGrant(
                    tool_name="write_fixture",
                    allowed_effects=(ToolEffect.WRITE,),
                    resource_patterns=("fixture:*",),
                    max_calls=2,
                ),
            ),
            filesystem_policy="WORKSPACE_WRITE",
        )
        first_request = replace(
            make_request(request_id="effect-owner"),
            action_policy=policy,
        )
        second_request = replace(
            make_request(request_id="effect-contender"),
            task=replace(make_request().task, job_id="job-2", task_id="task-2"),
            action_policy=policy,
        )
        first_handle, _ = store.create_run(first_request)
        second_handle, _ = store.create_run(second_request)
        store.begin_run(first_handle.run_id)
        store.begin_run(second_handle.run_id)
        executor = ToolExecutor(registry, store)

        def execute(handle, call, prior_tool_calls=0):  # type: ignore[no-untyped-def]
            return executor.execute(
                run_id=handle.run_id,
                model_call_index=1,
                call=call,
                policy=policy,
                cancellation=CancellationToken(),
                prior_tool_calls=prior_tool_calls,
                max_result_bytes=first_request.limits.max_result_bytes,
                max_tool_output_bytes=first_request.limits.max_tool_output_bytes,
                current_usage=store.get_usage(handle.run_id),
                remaining_wall_ms=1_000,
            )

        owner_task = asyncio.create_task(
            execute(first_handle, ToolCall("owner", "write_fixture", {}))
        )
        await started.wait()
        busy = await execute(second_handle, ToolCall("contender", "write_fixture", {}))

        self.assertFalse(busy.ok)
        self.assertEqual(busy.error_code, "RESOURCE_BUSY")
        self.assertEqual(handler_calls, 1)

        release.set()
        owner = await owner_task
        retry = await execute(
            second_handle,
            ToolCall("contender-retry", "write_fixture", {}),
            prior_tool_calls=1,
        )

        self.assertTrue(owner.ok)
        self.assertTrue(retry.ok)
        self.assertEqual(handler_calls, 2)
        store.close()

    async def test_duplicate_start_reuses_the_same_run(self) -> None:
        store = RunStore()
        provider = ScriptedModelProvider(
            [ModelResponse(completion=completion("only once"))],
            blocked_calls=(0,),
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=ToolRegistry())
        request = make_request()

        first = await service.start(request)
        await provider.wait_until_started(0)
        second = await service.start(request)
        provider.release(0)
        result = await service.collect(first)

        self.assertEqual(first, second)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(
            sum(event.type == EventType.RUN_CREATED for event in store.list_events(first.run_id)),
            1,
        )
        store.close()

    async def test_duplicate_action_replays_stored_result_without_handler(self) -> None:
        store = RunStore()
        request = make_request(tool_names=("read_fixture",), resource_patterns=("fixture:*",))
        handle, _ = store.create_run(request)
        store.begin_run(handle.run_id)
        fixture = FixtureReader({"bug": "evidence"})
        registry = ToolRegistry()
        registry.register(fixture.definition())
        executor = ToolExecutor(registry, store)
        call = ToolCall("same-call", "read_fixture", {"key": "bug"})

        first = await executor.execute(
            run_id=handle.run_id,
            model_call_index=1,
            call=call,
            policy=request.action_policy,
            cancellation=CancellationToken(),
            prior_tool_calls=0,
            max_result_bytes=request.limits.max_result_bytes,
            max_tool_output_bytes=request.limits.max_tool_output_bytes,
            current_usage=Usage(),
            remaining_wall_ms=1_000,
        )
        second = await executor.execute(
            run_id=handle.run_id,
            model_call_index=1,
            call=call,
            policy=request.action_policy,
            cancellation=CancellationToken(),
            prior_tool_calls=1,
            max_result_bytes=request.limits.max_result_bytes,
            max_tool_output_bytes=request.limits.max_tool_output_bytes,
            current_usage=store.get_usage(handle.run_id),
            remaining_wall_ms=1_000,
        )

        self.assertTrue(first.ok)
        self.assertTrue(second.replayed)
        self.assertEqual(fixture.call_count, 1)
        self.assertEqual(store.get_usage(handle.run_id).tool_calls, 1)
        self.assertEqual(
            sum(event.type == EventType.TOOL_INTENT_RECORDED for event in store.list_events(handle.run_id)),
            1,
        )
        store.close()

    async def test_action_key_conflict_is_rejected_without_second_handler_call(self) -> None:
        store = RunStore()
        request = make_request(tool_names=("read_fixture",), resource_patterns=("fixture:*",))
        handle, _ = store.create_run(request)
        store.begin_run(handle.run_id)
        fixture = FixtureReader({"first": "one", "second": "two"})
        registry = ToolRegistry()
        registry.register(fixture.definition())
        executor = ToolExecutor(registry, store)

        await executor.execute(
            run_id=handle.run_id,
            model_call_index=1,
            call=ToolCall("same-call", "read_fixture", {"key": "first"}),
            policy=request.action_policy,
            cancellation=CancellationToken(),
            prior_tool_calls=0,
            max_result_bytes=request.limits.max_result_bytes,
            max_tool_output_bytes=request.limits.max_tool_output_bytes,
            current_usage=Usage(),
            remaining_wall_ms=1_000,
        )
        with self.assertRaisesRegex(ToolExecutionError, "different tool call"):
            await executor.execute(
                run_id=handle.run_id,
                model_call_index=1,
                call=ToolCall("same-call", "read_fixture", {"key": "second"}),
                policy=request.action_policy,
                cancellation=CancellationToken(),
                prior_tool_calls=1,
                max_result_bytes=request.limits.max_result_bytes,
                max_tool_output_bytes=request.limits.max_tool_output_bytes,
                current_usage=store.get_usage(handle.run_id),
                remaining_wall_ms=1_000,
            )

        self.assertEqual(fixture.call_count, 1)
        self.assertEqual(store.get_usage(handle.run_id).tool_calls, 1)
        store.close()

    async def test_request_key_conflict_is_rejected_before_a_second_run(self) -> None:
        store = RunStore()
        provider = ScriptedModelProvider(
            [ModelResponse(completion=completion("only once"))],
            blocked_calls=(0,),
        )
        service = NativeEmployeeRuntimeService(store=store, provider=provider, registry=ToolRegistry())
        request = make_request()
        conflicting = replace(
            request,
            task=replace(request.task, objective="A different objective under the same key"),
        )

        first = await service.start(request)
        await provider.wait_until_started(0)
        with self.assertRaisesRegex(ValueError, "different request snapshot"):
            await service.start(conflicting)
        provider.release(0)
        await service.collect(first)

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(len(store.list_events(first.run_id)), 6)
        store.close()


if __name__ == "__main__":
    unittest.main()
