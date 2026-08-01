from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import unittest

from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.multi_route_job_plan import (
    DependencyArtifactHandoff,
    MultiRouteJobPlan,
    TaskRouteAssignment,
)
from dynamic_firm.company.multi_route_runtime_policy import MultiRouteRuntimePolicy
from dynamic_firm.company.route_provider_registry import (
    FrozenRouteProviderRegistry,
    RouteProviderDefinition,
)
from dynamic_firm.runtime.models import ModelRequest, ModelResponse
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry
from tests.runtime.helpers import completion, make_request


def binding(route_id: str, config_digest: str) -> ExecutionRouteBinding:
    values: dict[str, object] = {
        "attempt_id": f"attempt-{route_id}",
        "route_id": route_id,
        "execution_profile_id": f"profile-{route_id}",
        "provider_config_digest": config_digest,
        "credential_reference": "NORUCT_PROVIDER_KEY",
        "requested_model_id": f"model-{route_id}",
        "identity_assurance": "VERSIONED_MODEL_ID",
    }
    values.update(
        {
            name: "b" * 64
            for name in (
                "required_capability_digest",
                "inference_contract_digest",
                "egress_policy_digest",
                "intelligence_snapshot_digest",
                "orchestration_policy_digest",
                "compatibility_evidence_digest",
                "fallback_policy_digest",
                "fanout_policy_digest",
                "continuation_policy_digest",
            )
        }
    )
    return ExecutionRouteBinding(**values)


class RecordingProvider:
    def __init__(self, label: str, binding: ExecutionRouteBinding) -> None:
        self.label = label
        self.binding = binding
        self.calls: list[ModelRequest] = []

    async def complete(
        self, request: ModelRequest, _cancellation: CancellationToken
    ) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(completion=completion(self.label))

    async def complete_stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
        _progress: callable,
    ) -> ModelResponse:
        return await self.complete(request, cancellation)


class MultiRouteRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_frozen_tasks_use_only_their_durable_exact_route(self) -> None:
        explore = binding("route-explore", "a" * 64)
        verify = binding("route-verify", "c" * 64)
        integrate = binding("route-integrate", "d" * 64)
        plan = MultiRouteJobPlan(
            "e" * 64,
            (
                TaskRouteAssignment("explore", "employee-explore", explore.digest),
                TaskRouteAssignment(
                    "verify", "employee-verify", verify.digest, ("explore",)
                ),
                TaskRouteAssignment(
                    "integrate",
                    "employee-integrate",
                    integrate.digest,
                    ("verify",),
                    True,
                ),
            ),
            (
                DependencyArtifactHandoff("explore", "verify", "f" * 64),
                DependencyArtifactHandoff("verify", "integrate", "0" * 64),
            ),
            "employee-integrate",
        )
        policy = MultiRouteRuntimePolicy(plan, (verify, integrate, explore))
        created: dict[str, list[RecordingProvider]] = {
            "explore": [],
            "verify": [],
            "integrate": [],
        }

        def factory(label: str):
            def construct(value: ExecutionRouteBinding) -> RecordingProvider:
                provider = RecordingProvider(label, value)
                created[label].append(provider)
                return provider

            return construct

        registry = FrozenRouteProviderRegistry(
            (
                RouteProviderDefinition(
                    explore.route_id, explore.provider_config_digest,
                    explore.credential_reference, factory("explore"),
                ),
                RouteProviderDefinition(
                    verify.route_id, verify.provider_config_digest,
                    verify.credential_reference, factory("verify"),
                ),
                RouteProviderDefinition(
                    integrate.route_id, integrate.provider_config_digest,
                    integrate.credential_reference, factory("integrate"),
                ),
            )
        )
        default_provider = RecordingProvider("default", explore)
        store = RunStore()
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=default_provider,
            registry=ToolRegistry(),
            frozen_route_binding_resolver=policy,
            frozen_route_registry=registry,
        )
        def request_for(task_id: str, employee_id: str):
            base = make_request(request_id=f"three-route-{task_id}")
            selected_memory = replace(
                base.context.selected_memory[0],
                content_id=f"employee-memory:{employee_id}:fact-1",
            )
            employee = replace(
                base.employee,
                employee_id=employee_id,
                model_profile=f"attacker-controlled-{task_id}",
                memory_namespace=f"employee:{employee_id}",
                skills=(
                    replace(
                        base.employee.skills[0],
                        content_id=f"employee-skill:{employee_id}:read-evidence:fixture",
                    ),
                ),
                selected_memory_refs=(selected_memory.content_id,),
            )
            return replace(
                base,
                employee=employee,
                context=replace(base.context, selected_memory=(selected_memory,)),
                task=replace(base.task, job_id="three-route-job", task_id=task_id),
            )

        requests = tuple(
            request_for(task_id, employee_id)
            for task_id, employee_id in (
                ("explore", "employee-explore"),
                ("verify", "employee-verify"),
                ("integrate", "employee-integrate"),
            )
        )
        try:
            bad = replace(
                requests[0],
                employee=replace(requests[0].employee, employee_id="employee-drift"),
            )
            with self.assertRaises(ValueError):
                await service.start(bad)
            self.assertTrue(all(not providers for providers in created.values()))

            results = []
            for request in requests:
                results.append(await service.collect(await service.start(request)))
            results = tuple(results)

            self.assertEqual(
                tuple(result.status.value for result in results),
                ("SUCCEEDED",) * 3,
                tuple(result.failure for result in results),
            )
            self.assertEqual(len(default_provider.calls), 0)
            self.assertEqual(
                tuple(provider.binding for label in ("explore", "verify", "integrate")
                      for provider in created[label]),
                (explore, verify, integrate),
            )
            self.assertEqual(
                tuple(provider.calls[0].model_profile for label in ("explore", "verify", "integrate")
                      for provider in created[label]),
                tuple(binding.requested_model_id for binding in (explore, verify, integrate)),
            )
            self.assertEqual(
                tuple(store.get_frozen_route_binding(result.run_id) for result in results),
                (explore, verify, integrate),
            )
            self.assertEqual(plan.acting_integrator_id, "employee-integrate")
            with self.assertRaises(FrozenInstanceError):
                plan.acting_integrator_id = "employee-explore"  # type: ignore[misc]
            self.assertFalse(any(hasattr(plan, name) for name in ("apply", "mutate", "replace_graph")))
        finally:
            await service.close()
            store.close()


if __name__ == "__main__":
    unittest.main()
