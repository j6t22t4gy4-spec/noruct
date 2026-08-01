from __future__ import annotations

import unittest
from dataclasses import replace

from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.frozen_route_admission import FrozenRouteAdmission
from dynamic_firm.company.route_provider_registry import (
    FrozenRouteProviderRegistry,
    RouteProviderDefinition,
)
from dynamic_firm.company.route_selection_receipt import (
    RouteCandidateReceipt,
    RouteSelectionReceipt,
    SelectionReason,
)
from dynamic_firm.runtime.models import (
    ModelRequest,
    ModelResponse,
    ModelStreamProgress,
    StructuredOutputRequest,
    StructuredOutputResponse,
)
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


def admission(value: ExecutionRouteBinding) -> FrozenRouteAdmission:
    return FrozenRouteAdmission(
        binding=value,
        selection_receipt=RouteSelectionReceipt(
            candidates=(RouteCandidateReceipt(value.route_id),),
            selected_route_id=value.route_id,
            selection_reasons=(SelectionReason.HARD_CONSTRAINTS_SATISFIED,),
            policy_digest=value.orchestration_policy_digest,
        ),
    )


class RecordingProvider:
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[object] = []

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(completion=completion(self.label))

    async def complete_stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
        progress: callable,
    ) -> ModelResponse:
        progress(ModelStreamProgress(1, 1, True))
        return await self.complete(request, cancellation)

    async def complete_structured(
        self, request: StructuredOutputRequest, cancellation: CancellationToken
    ) -> StructuredOutputResponse:
        self.calls.append(request)
        return StructuredOutputResponse(value={"route": self.label})


class FrozenRouteServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.first = binding("route-a", "a" * 64)
        self.second = binding("route-b", "c" * 64)
        self.created: dict[str, list[RecordingProvider]] = {"a": [], "b": []}

        def factory(label: str):
            def build(value: ExecutionRouteBinding) -> RecordingProvider:
                provider = RecordingProvider(label)
                self.created[label].append(provider)
                return provider

            return build

        self.registry = FrozenRouteProviderRegistry(
            (
                RouteProviderDefinition("route-a", "a" * 64, "NORUCT_PROVIDER_KEY", factory("a")),
                RouteProviderDefinition("route-b", "c" * 64, "NORUCT_PROVIDER_KEY", factory("b")),
            )
        )

    async def test_default_mode_never_calls_binding_resolver_or_changes_provider(self) -> None:
        store = RunStore()
        global_provider = RecordingProvider("global")
        resolver_calls = 0

        def resolver(_request):  # type: ignore[no-untyped-def]
            nonlocal resolver_calls
            resolver_calls += 1
            return self.first

        service = NativeEmployeeRuntimeService(
            store=store,
            provider=global_provider,
            registry=ToolRegistry(),
        )
        try:
            result = await service.collect(await service.start(make_request()))
            self.assertEqual(result.status.value, "SUCCEEDED")
            self.assertEqual(resolver_calls, 0)
            self.assertEqual(len(global_provider.calls), 1)
        finally:
            await service.close()
            store.close()

    async def test_frozen_runs_use_exact_routes_not_request_model_profile(self) -> None:
        store = RunStore()
        global_provider = RecordingProvider("global")
        bindings = {"one": self.first, "two": self.second}
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=global_provider,
            registry=ToolRegistry(),
            frozen_route_binding_resolver=lambda request: bindings[request.request_id],
            frozen_route_registry=self.registry,
        )
        try:
            one = replace(
                make_request(request_id="one"),
                employee=replace(make_request().employee, model_profile="attacker-controlled"),
            )
            two = replace(make_request(request_id="two"), task=replace(make_request().task, task_id="task-2"))
            first_result = await service.collect(await service.start(one))
            second_result = await service.collect(await service.start(two))
            self.assertEqual((first_result.status.value, second_result.status.value), ("SUCCEEDED", "SUCCEEDED"))
            self.assertEqual(len(global_provider.calls), 0)
            self.assertEqual([item.label for item in self.created["a"]], ["a"])
            self.assertEqual([item.label for item in self.created["b"]], ["b"])
            self.assertEqual(
                self.created["a"][0].calls[0].model_profile,
                self.first.requested_model_id,
            )
            self.assertEqual(store.get_frozen_route_binding(first_result.run_id), self.first)
            self.assertEqual(store.get_frozen_route_binding(second_result.run_id), self.second)
        finally:
            await service.close()
            store.close()

    async def test_admission_resolver_persists_exact_route_and_receipt(self) -> None:
        store = RunStore()
        selected = admission(self.second)
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=RecordingProvider("global"),
            registry=ToolRegistry(),
            frozen_route_admission_resolver=lambda _request: selected,
            frozen_route_registry=self.registry,
        )
        try:
            result = await service.collect(await service.start(make_request()))
            self.assertEqual(result.status.value, "SUCCEEDED")
            self.assertEqual(store.get_frozen_route_binding(result.run_id), self.second)
            self.assertEqual(store.get_frozen_route_admission(result.run_id), selected)
            self.assertEqual([item.label for item in self.created["b"]], ["b"])
            self.assertEqual(self.created["a"], [])
        finally:
            await service.close()
            store.close()

    async def test_binding_and_admission_mismatch_fails_before_factory(self) -> None:
        store = RunStore()
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=RecordingProvider("global"),
            registry=ToolRegistry(),
            frozen_route_binding_resolver=lambda _request: self.first,
            frozen_route_admission_resolver=lambda _request: admission(self.second),
            frozen_route_registry=self.registry,
        )
        try:
            with self.assertRaises(ValueError):
                await service.start(make_request())
            self.assertEqual(self.created, {"a": [], "b": []})
        finally:
            await service.close()
            store.close()

    async def test_malformed_admission_return_fails_before_factory(self) -> None:
        store = RunStore()
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=RecordingProvider("global"),
            registry=ToolRegistry(),
            frozen_route_admission_resolver=lambda _request: object(),  # type: ignore[return-value]
            frozen_route_registry=self.registry,
        )
        try:
            with self.assertRaises(TypeError):
                await service.start(make_request())
            self.assertEqual(self.created, {"a": [], "b": []})
        finally:
            await service.close()
            store.close()

    async def test_idempotent_binding_drift_fails_before_any_second_provider_call(self) -> None:
        store = RunStore()
        selected = self.first
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=RecordingProvider("global"),
            registry=ToolRegistry(),
            frozen_route_binding_resolver=lambda _request: selected,
            frozen_route_registry=self.registry,
        )
        request = make_request(request_id="drift")
        try:
            await service.collect(await service.start(request))
            selected = self.second
            with self.assertRaises(ValueError):
                await service.start(request)
            self.assertEqual(len(self.created["a"][0].calls), 1)
            self.assertEqual(self.created["b"], [])
        finally:
            await service.close()
            store.close()

    async def test_structured_physical_request_id_resolves_from_durable_store(self) -> None:
        store = RunStore()
        service = NativeEmployeeRuntimeService(
            store=store,
            provider=RecordingProvider("global"),
            registry=ToolRegistry(),
            frozen_route_binding_resolver=lambda _request: self.first,
            frozen_route_registry=self.registry,
        )
        request = make_request(request_id="structured-request")
        try:
            await service.collect(await service.start(request))
            assert service._frozen_provider is not None  # noqa: SLF001 - service seam contract
            response = await service._frozen_provider.complete_structured(  # noqa: SLF001
                StructuredOutputRequest((), "result", {}, "attacker-controlled", request.request_id),
                CancellationToken(),
            )
            self.assertEqual(response.value, {"route": "a"})
        finally:
            await service.close()
            store.close()


if __name__ == "__main__":
    unittest.main()
