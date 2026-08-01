from __future__ import annotations

import asyncio
import unittest

from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.frozen_route_admission import FrozenRouteAdmission
from dynamic_firm.company.frozen_task_route_provider import FrozenTaskRouteProvider
from dynamic_firm.company.route_provider_registry import (
    FrozenRouteProviderRegistry,
    RouteProviderDefinition,
)
from dynamic_firm.runtime.models import (
    ModelRequest,
    ModelResponse,
    StructuredOutputRequest,
    StructuredOutputResponse,
)
from dynamic_firm.runtime.ports import CancellationToken, ModelProviderError, OperationCancelled
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.company.route_selection_receipt import (
    RouteCandidateReceipt,
    RouteSelectionReceipt,
    SelectionReason,
)
from tests.runtime.helpers import make_request


def binding(route_id: str, *, config_digest: str) -> ExecutionRouteBinding:
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
            selection_reasons=(SelectionReason.POLICY_ORDER,),
            policy_digest=value.orchestration_policy_digest,
        ),
    )


class RecordingProvider:
    def __init__(self, label: str, created_for: ExecutionRouteBinding) -> None:
        self.label = label
        self.created_for = created_for
        self.calls: list[object] = []

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(content=self.label)

    async def complete_stream(self, request, cancellation, progress) -> ModelResponse:
        self.calls.append(request)
        progress(type("Progress", (), {"chunk_count": 1, "received_chars": 1, "finished": True})())
        return ModelResponse(content=f"stream-{self.label}")

    async def complete_structured(
        self, request: StructuredOutputRequest, cancellation: CancellationToken
    ) -> StructuredOutputResponse:
        self.calls.append(request)
        return StructuredOutputResponse(value={"route": self.label})


class BlockingRequestIdentifyingProvider(RecordingProvider):
    def __init__(self, created_for: ExecutionRouteBinding) -> None:
        super().__init__("blocking", created_for)
        self.started = asyncio.Event()
        self._cancelled_ids: dict[str, str] = {}

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        self.calls.append(request)
        self._cancelled_ids[request.run_id] = "adapter-cancelled-request-id"
        self.started.set()
        await cancellation.wait()
        raise OperationCancelled(cancellation.reason or "cancelled")

    def consume_cancelled_request_id(self, physical_id: str) -> str | None:
        return self._cancelled_ids.pop(physical_id, None)


class FrozenTaskRouteProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding_a = binding("route-a", config_digest="a" * 64)
        self.binding_b = binding("route-b", config_digest="c" * 64)
        self.created: list[RecordingProvider] = []

        def factory(label: str):
            def build(value: ExecutionRouteBinding) -> RecordingProvider:
                provider = RecordingProvider(label, value)
                self.created.append(provider)
                return provider

            return build

        self.registry = FrozenRouteProviderRegistry(
            (
                RouteProviderDefinition("route-a", "a" * 64, "NORUCT_PROVIDER_KEY", factory("a")),
                RouteProviderDefinition("route-b", "c" * 64, "NORUCT_PROVIDER_KEY", factory("b")),
            )
        )
        self.resolved: list[str] = []
        self.bindings = {
            "run-a": self.binding_a,
            "run-b": self.binding_b,
            "structured-a": self.binding_a,
        }

        def resolve(physical_id: str) -> ExecutionRouteBinding:
            self.resolved.append(physical_id)
            return self.bindings[physical_id]

        self.provider = FrozenTaskRouteProvider(resolve, self.registry)

    def request(self, run_id: str) -> ModelRequest:
        return ModelRequest((), (), "attacker-controlled-profile", run_id, 1)

    def test_concurrent_calls_construct_only_each_runs_frozen_binding(self) -> None:
        async def call_all() -> tuple[ModelResponse, ModelResponse]:
            return await asyncio.gather(
                self.provider.complete(self.request("run-a"), CancellationToken()),
                self.provider.complete(self.request("run-b"), CancellationToken()),
            )

        first, second = asyncio.run(call_all())
        self.assertEqual((first.content, second.content), ("a", "b"))
        self.assertEqual(self.resolved, ["run-a", "run-b"])
        self.assertEqual([item.created_for for item in self.created], [self.binding_a, self.binding_b])
        self.assertEqual(
            [item.calls[0].model_profile for item in self.created],
            [self.binding_a.requested_model_id, self.binding_b.requested_model_id],
        )

    def test_cancelled_preflight_does_not_resolve_or_construct(self) -> None:
        token = CancellationToken()
        token.cancel("already cancelled")
        with self.assertRaises(OperationCancelled):
            asyncio.run(self.provider.complete(self.request("run-a"), token))
        self.assertEqual(self.resolved, [])
        self.assertEqual(self.created, [])

    def test_unresolvable_or_incompatible_adapter_fails_closed(self) -> None:
        with self.assertRaises(ModelProviderError) as unresolved:
            asyncio.run(self.provider.complete(self.request("unknown"), CancellationToken()))
        self.assertEqual(unresolved.exception.code, "FROZEN_ROUTE_UNAVAILABLE")

        no_complete_registry = FrozenRouteProviderRegistry(
            (
                RouteProviderDefinition(
                    "route-a", "a" * 64, "NORUCT_PROVIDER_KEY", lambda _: object()
                ),
            )
        )
        no_complete = FrozenTaskRouteProvider(lambda _: self.binding_a, no_complete_registry)
        with self.assertRaises(ModelProviderError) as incompatible:
            asyncio.run(no_complete.complete(self.request("run-a"), CancellationToken()))
        self.assertEqual(incompatible.exception.code, "FROZEN_ROUTE_ADAPTER_INVALID")

    def test_streaming_and_structured_calls_preserve_exact_frozen_binding(self) -> None:
        progress = []
        stream = asyncio.run(
            self.provider.complete_stream(self.request("run-a"), CancellationToken(), progress.append)
        )
        structured = asyncio.run(
            self.provider.complete_structured(
                StructuredOutputRequest((), "result", {}, "non-authoritative", "structured-a"),
                CancellationToken(),
            )
        )
        self.assertEqual(stream.content, "stream-a")
        self.assertEqual(structured.value, {"route": "a"})
        self.assertEqual(self.resolved, ["run-a", "structured-a"])
        self.assertEqual([item.created_for for item in self.created], [self.binding_a, self.binding_a])
        self.assertEqual(
            [item.calls[0].model_profile for item in self.created],
            [self.binding_a.requested_model_id, self.binding_a.requested_model_id],
        )
        self.assertEqual(len(progress), 1)

    def test_admission_required_dispatch_resolves_one_verified_durable_pair(self) -> None:
        store = RunStore()
        try:
            expected = admission(self.binding_a)
            request = make_request(request_id="durable-physical-id")
            handle, _ = store.create_run(request, frozen_route_admission=expected)
            provider = FrozenTaskRouteProvider(
                store.resolve_frozen_route_binding,
                self.registry,
                resolve_admission=store.resolve_frozen_route_admission,
            )

            response = asyncio.run(
                provider.complete(
                    self.request(handle.run_id), CancellationToken()
                )
            )
            self.assertEqual(response.content, "a")
            self.assertEqual(self.created[-1].calls[0].model_profile, "model-route-a")
            self.assertEqual(
                store.resolve_frozen_route_admission(request.request_id), expected
            )

            legacy, _ = store.create_run(
                make_request(request_id="binding-only-physical-id"),
                frozen_route_binding=self.binding_a,
            )
            before = len(self.created)
            with self.assertRaises(ModelProviderError) as missing:
                asyncio.run(provider.complete(self.request(legacy.run_id), CancellationToken()))
            self.assertEqual(missing.exception.code, "FROZEN_ROUTE_UNAVAILABLE")
            self.assertEqual(len(self.created), before)
        finally:
            store.close()

    def test_cancelled_call_preserves_the_exact_adapter_receipt_without_reselecting(self) -> None:
        adapter = BlockingRequestIdentifyingProvider(self.binding_a)
        constructed: list[ExecutionRouteBinding] = []
        resolved: list[str] = []

        def resolve(physical_id: str) -> ExecutionRouteBinding:
            resolved.append(physical_id)
            return self.binding_a

        registry = FrozenRouteProviderRegistry(
            (
                RouteProviderDefinition(
                    "route-a",
                    "a" * 64,
                    "NORUCT_PROVIDER_KEY",
                    lambda value: (constructed.append(value), adapter)[1],
                ),
            )
        )
        provider = FrozenTaskRouteProvider(resolve, registry)

        async def cancel_in_flight() -> None:
            token = CancellationToken()
            call = asyncio.create_task(provider.complete(self.request("run-a"), token))
            await adapter.started.wait()
            token.cancel("caller cancelled")
            with self.assertRaises(OperationCancelled):
                await call

        asyncio.run(cancel_in_flight())
        before_consume = (list(resolved), list(constructed))
        self.assertEqual(
            provider.consume_cancelled_request_id("run-a"),
            "adapter-cancelled-request-id",
        )
        self.assertEqual((resolved, constructed), before_consume)
        self.assertIsNone(provider.consume_cancelled_request_id("run-a"))

    def test_direct_task_cancellation_clears_adapter_state_without_receipt(self) -> None:
        adapter = BlockingRequestIdentifyingProvider(self.binding_a)
        constructed: list[ExecutionRouteBinding] = []
        resolved: list[str] = []

        def resolve(physical_id: str) -> ExecutionRouteBinding:
            resolved.append(physical_id)
            return self.binding_a

        registry = FrozenRouteProviderRegistry(
            (
                RouteProviderDefinition(
                    "route-a",
                    "a" * 64,
                    "NORUCT_PROVIDER_KEY",
                    lambda value: (constructed.append(value), adapter)[1],
                ),
            )
        )
        provider = FrozenTaskRouteProvider(resolve, registry)

        async def cancel_task() -> None:
            call = asyncio.create_task(
                provider.complete(self.request("run-a"), CancellationToken())
            )
            await adapter.started.wait()
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call

        asyncio.run(cancel_task())
        self.assertEqual(provider._inflight_adapters, {})
        self.assertEqual(provider._cancelled_adapters, {})
        before_consume = (list(resolved), list(constructed))
        self.assertIsNone(provider.consume_cancelled_request_id("run-a"))
        self.assertEqual((resolved, constructed), before_consume)


if __name__ == "__main__":
    unittest.main()
