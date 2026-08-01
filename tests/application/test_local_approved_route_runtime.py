from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.application.local_approved_route_runtime import (
    LocalApprovedRouteRuntime,
    PreFrozenSelectionReceipt,
)
from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.multi_route_job_plan import MultiRouteJobPlan, TaskRouteAssignment
from dynamic_firm.company.multi_route_runtime_policy import MultiRouteRuntimePolicy
from dynamic_firm.company.route_provider_registry import (
    FrozenRouteProviderRegistry,
    RouteProviderDefinition,
)
from dynamic_firm.company.route_selection_receipt import (
    RouteCandidateReceipt,
    RouteSelectionReceipt,
    SelectionReason,
)
from dynamic_firm.company.user_routing_policy import (
    ApprovedRouteMetadata,
    ApprovedRouteRegistry,
    UserRoutingPolicy,
    UserRoutingPolicyMode,
)
from dynamic_firm.product.local_routing_settings import (
    LocalRoutingSettings,
    write_local_routing_settings,
)
from dynamic_firm.runtime.models import ModelRequest, ModelResponse
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.service import NativeEmployeeRuntimeService
from dynamic_firm.runtime.store import RunStore
from dynamic_firm.runtime.tools import ToolRegistry
from tests.runtime.helpers import completion, make_request


def binding() -> ExecutionRouteBinding:
    values: dict[str, object] = {
        "attempt_id": "attempt-local-approved",
        "route_id": "local-approved-route",
        "execution_profile_id": "profile-local-approved",
        "provider_config_digest": "a" * 64,
        "credential_reference": "APPROVED_ROUTE_KEY",
        "requested_model_id": "local-approved-model",
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
    def __init__(self) -> None:
        self.calls: list[ModelRequest] = []

    async def complete(
        self, request: ModelRequest, _cancellation: CancellationToken
    ) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(completion=completion("local-approved"))

    async def complete_stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
        _progress: callable,
    ) -> ModelResponse:
        return await self.complete(request, cancellation)


class LocalApprovedRouteRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.binding = binding()
        self.request = make_request(request_id="local-approved-one")
        self.selection_receipt = RouteSelectionReceipt(
            candidates=(RouteCandidateReceipt(self.binding.route_id),),
            selected_route_id=self.binding.route_id,
            selection_reasons=(SelectionReason.HARD_CONSTRAINTS_SATISFIED,),
            policy_digest=self.binding.orchestration_policy_digest,
        )
        self.policy = MultiRouteRuntimePolicy(
            MultiRouteJobPlan(
                "c" * 64,
                (
                    TaskRouteAssignment(
                        self.request.task.task_id,
                        self.request.employee.employee_id,
                        self.binding.digest,
                        final=True,
                        expected_selection_receipt_digest=self.selection_receipt.digest,
                    ),
                ),
                (),
                self.request.employee.employee_id,
            ),
            (self.binding,),
        )
        self.settings = LocalRoutingSettings(
            UserRoutingPolicy(UserRoutingPolicyMode.BALANCED),
            ApprovedRouteRegistry(
                (
                    ApprovedRouteMetadata(
                        self.binding.route_id,
                        self.binding.digest,
                        self.binding.provider_config_digest,
                        self.binding.credential_reference,
                    ),
                )
            ),
        )

    def frozen_receipts(self) -> tuple[PreFrozenSelectionReceipt, ...]:
        return (
            PreFrozenSelectionReceipt(
                self.binding.digest,
                self.selection_receipt,
            ),
        )

    async def test_runtime_reloads_equally_valid_policy_and_dispatches_exact_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            write_local_routing_settings(path, self.settings)
            resolver = LocalApprovedRouteRuntime(path, self.policy)
            created: list[RecordingProvider] = []

            def factory(_binding: ExecutionRouteBinding) -> RecordingProvider:
                provider = RecordingProvider()
                created.append(provider)
                return provider

            frozen_registry = FrozenRouteProviderRegistry(
                (
                    RouteProviderDefinition(
                        self.binding.route_id,
                        self.binding.provider_config_digest,
                        self.binding.credential_reference,
                        factory,
                    ),
                )
            )
            default = RecordingProvider()
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=default,
                registry=ToolRegistry(),
                frozen_route_binding_resolver=resolver,
                frozen_route_registry=frozen_registry,
            )
            try:
                first = await service.collect(await service.start(self.request))
                self.assertEqual(first.status.value, "SUCCEEDED")
                self.assertEqual(store.get_frozen_route_binding(first.run_id), self.binding)
                self.assertEqual(len(default.calls), 0)
                self.assertEqual(len(created), 1)

                write_local_routing_settings(
                    path,
                    LocalRoutingSettings(
                        UserRoutingPolicy(UserRoutingPolicyMode.EFFICIENT),
                        self.settings.approved_routes,
                    ),
                )
                second_request = replace(self.request, request_id="local-approved-two")
                second = await service.collect(await service.start(second_request))
                self.assertEqual(second.status.value, "SUCCEEDED")
                self.assertEqual(store.get_frozen_route_binding(second.run_id), self.binding)
                self.assertEqual(len(default.calls), 0)
                self.assertEqual([len(provider.calls) for provider in created], [1, 1])
            finally:
                await service.close()
                store.close()

    async def test_missing_empty_drift_and_invalid_settings_fail_before_provider_factory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            resolver = LocalApprovedRouteRuntime(path, self.policy, self.frozen_receipts())
            factory_calls = 0

            def factory(_binding: ExecutionRouteBinding) -> RecordingProvider:
                nonlocal factory_calls
                factory_calls += 1
                return RecordingProvider()

            frozen_registry = FrozenRouteProviderRegistry(
                (
                    RouteProviderDefinition(
                        self.binding.route_id,
                        self.binding.provider_config_digest,
                        self.binding.credential_reference,
                        factory,
                    ),
                )
            )
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=RecordingProvider(),
                registry=ToolRegistry(),
                frozen_route_admission_resolver=resolver.admission_for,
                frozen_route_registry=frozen_registry,
            )
            try:
                with self.assertRaises(ValueError):
                    await service.start(self.request)
                write_local_routing_settings(
                    path,
                    LocalRoutingSettings(self.settings.policy, ApprovedRouteRegistry(())),
                )
                with self.assertRaises(ValueError):
                    await service.start(self.request)
                drift = ApprovedRouteMetadata(
                    self.binding.route_id,
                    "d" * 64,
                    self.binding.provider_config_digest,
                    self.binding.credential_reference,
                )
                write_local_routing_settings(
                    path,
                    LocalRoutingSettings(self.settings.policy, ApprovedRouteRegistry((drift,))),
                )
                with self.assertRaises(ValueError):
                    await service.start(self.request)
                path.write_text(
                    '[model_routing]\npolicy = "{\\"mode\\":\\"UNKNOWN\\"}"\napproved_routes = "{\\"routes\\":[]}"\n',
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    await service.start(self.request)
                self.assertEqual(factory_calls, 0)
            finally:
                await service.close()
                store.close()

    async def test_admission_resolver_persists_pre_frozen_receipt_and_dispatches_exact_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            write_local_routing_settings(path, self.settings)
            resolver = LocalApprovedRouteRuntime(path, self.policy, self.frozen_receipts())
            created: list[RecordingProvider] = []

            def factory(_binding: ExecutionRouteBinding) -> RecordingProvider:
                provider = RecordingProvider()
                created.append(provider)
                return provider

            frozen_registry = FrozenRouteProviderRegistry(
                (
                    RouteProviderDefinition(
                        self.binding.route_id,
                        self.binding.provider_config_digest,
                        self.binding.credential_reference,
                        factory,
                    ),
                )
            )
            default = RecordingProvider()
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=default,
                registry=ToolRegistry(),
                frozen_route_admission_resolver=resolver.admission_for,
                frozen_route_registry=frozen_registry,
            )
            try:
                result = await service.collect(await service.start(self.request))
                admission = resolver.admission_for(self.request)
                self.assertEqual(result.status.value, "SUCCEEDED")
                self.assertEqual(store.get_frozen_route_admission(result.run_id), admission)
                self.assertEqual(len(default.calls), 0)
                self.assertEqual(len(created), 1)
            finally:
                await service.close()
                store.close()

    def test_admission_receipts_must_have_exact_unique_binding_coverage(self) -> None:
        valid = self.frozen_receipts()[0]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            with self.assertRaises(ValueError):
                LocalApprovedRouteRuntime(path, self.policy, ())
            with self.assertRaises(ValueError):
                LocalApprovedRouteRuntime(path, self.policy, (valid, valid))
            foreign = PreFrozenSelectionReceipt("d" * 64, valid.selection_receipt)
            with self.assertRaises(ValueError):
                LocalApprovedRouteRuntime(path, self.policy, (foreign,))
            wrong_route = PreFrozenSelectionReceipt(
                self.binding.digest,
                RouteSelectionReceipt(
                    candidates=(RouteCandidateReceipt("foreign-route"),),
                    selected_route_id="foreign-route",
                    selection_reasons=(SelectionReason.HARD_CONSTRAINTS_SATISFIED,),
                    policy_digest=self.binding.orchestration_policy_digest,
                ),
            )
            resolver = LocalApprovedRouteRuntime(path, self.policy, (wrong_route,))
            write_local_routing_settings(path, self.settings)
            with self.assertRaises(ValueError):
                resolver.admission_for(self.request)

    def test_admission_resolution_rejects_legacy_plan_without_receipt_provenance(self) -> None:
        legacy_policy = MultiRouteRuntimePolicy(
            MultiRouteJobPlan(
                "c" * 64,
                (
                    TaskRouteAssignment(
                        self.request.task.task_id,
                        self.request.employee.employee_id,
                        self.binding.digest,
                        final=True,
                    ),
                ),
                (),
                self.request.employee.employee_id,
            ),
            (self.binding,),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            write_local_routing_settings(path, self.settings)
            resolver = LocalApprovedRouteRuntime(path, legacy_policy, self.frozen_receipts())
            with self.assertRaises(ValueError):
                resolver.admission_for(self.request)

    async def test_admission_rejects_different_receipt_digest_before_provider_factory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            write_local_routing_settings(path, self.settings)
            different_receipt = RouteSelectionReceipt(
                candidates=(
                    RouteCandidateReceipt(self.binding.route_id),
                    RouteCandidateReceipt("unavailable-route", uncertainty=1.0),
                ),
                selected_route_id=self.binding.route_id,
                selection_reasons=(SelectionReason.HARD_CONSTRAINTS_SATISFIED,),
                policy_digest=self.binding.orchestration_policy_digest,
            )
            resolver = LocalApprovedRouteRuntime(
                path,
                self.policy,
                (PreFrozenSelectionReceipt(self.binding.digest, different_receipt),),
            )
            factory_calls = 0

            def factory(_binding: ExecutionRouteBinding) -> RecordingProvider:
                nonlocal factory_calls
                factory_calls += 1
                return RecordingProvider()

            frozen_registry = FrozenRouteProviderRegistry(
                (
                    RouteProviderDefinition(
                        self.binding.route_id,
                        self.binding.provider_config_digest,
                        self.binding.credential_reference,
                        factory,
                    ),
                )
            )
            store = RunStore()
            service = NativeEmployeeRuntimeService(
                store=store,
                provider=RecordingProvider(),
                registry=ToolRegistry(),
                frozen_route_admission_resolver=resolver.admission_for,
                frozen_route_registry=frozen_registry,
            )
            try:
                with self.assertRaises(ValueError):
                    await service.start(self.request)
                self.assertEqual(factory_calls, 0)
            finally:
                await service.close()
                store.close()

    def test_requires_typed_path_policy_and_request(self) -> None:
        with self.assertRaises(TypeError):
            LocalApprovedRouteRuntime("config.toml", self.policy)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as temporary:
            resolver = LocalApprovedRouteRuntime(Path(temporary) / "config.toml", self.policy)
            with self.assertRaises(TypeError):
                resolver(object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
