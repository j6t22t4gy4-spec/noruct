from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from os.path import relpath
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dynamic_firm.application import goal_runtime
from dynamic_firm.application.frozen_route_goal_composition import (
    FrozenRouteGoalComposition,
)
from dynamic_firm.application.goal_completion_runtime import (
    GoalCompletionPorts,
    execute_admitted_goal,
)
from dynamic_firm.application.goal_runtime import _frozen_route_runtime_kwargs
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
from dynamic_firm.cli import RunCommandConfig
from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.kernel.models import EmployeeRecord, TaskAssignmentEvent
from dynamic_firm.kernel.mutation import graph_structure_digest
from dynamic_firm.product import InputRoute
from dynamic_firm.product.local_routing_settings import (
    LocalRoutingSettings,
    write_local_routing_settings,
)
from dynamic_firm.company.user_routing_policy import (
    ApprovedRouteMetadata,
    ApprovedRouteRegistry,
    UserRoutingPolicy,
    UserRoutingPolicyMode,
)
from tests.kernel.helpers import company_request, task
from tests.runtime.helpers import make_request


def _binding() -> ExecutionRouteBinding:
    values: dict[str, object] = {
        "attempt_id": "attempt-goal-composition",
        "route_id": "goal-composition-route",
        "execution_profile_id": "goal-composition-profile",
        "provider_config_digest": "a" * 64,
        "credential_reference": "GOAL_COMPOSITION_KEY",
        "requested_model_id": "goal-composition-model",
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


class FrozenRouteGoalCompositionTests(unittest.TestCase):
    def _company_request(self):
        return company_request(
            (task("task-1", capabilities=("repository_analysis",)),),
            final_task_id="task-1",
            roster=(
                EmployeeRecord(
                    "employee-researcher",
                    "Repository Analyst",
                    ("repository_analysis",),
                ),
            ),
        )

    def _composition(
        self,
        *,
        company_request_value=None,
    ) -> FrozenRouteGoalComposition:
        binding = _binding()
        request = make_request(request_id="goal-composition-request")
        receipt = RouteSelectionReceipt(
            candidates=(RouteCandidateReceipt(binding.route_id),),
            selected_route_id=binding.route_id,
            selection_reasons=(SelectionReason.HARD_CONSTRAINTS_SATISFIED,),
            policy_digest=binding.orchestration_policy_digest,
        )
        policy = MultiRouteRuntimePolicy(
            MultiRouteJobPlan(
                (
                    "c" * 64
                    if company_request_value is None
                    else graph_structure_digest(
                        graph_from_proposal(
                            company_request_value.plan_proposal,
                            max_tasks=company_request_value.job_limits.max_tasks,
                        )
                    )
                ),
                (
                    TaskRouteAssignment(
                        request.task.task_id,
                        request.employee.employee_id,
                        binding.digest,
                        final=True,
                        expected_selection_receipt_digest=receipt.digest,
                    ),
                ),
                (),
                request.employee.employee_id,
            ),
            (binding,),
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        runtime = LocalApprovedRouteRuntime(
            Path(directory.name) / "routing.toml",
            policy,
            (PreFrozenSelectionReceipt(binding.digest, receipt),),
        )
        write_local_routing_settings(
            runtime.config_path,
            LocalRoutingSettings(
                UserRoutingPolicy(UserRoutingPolicyMode.BALANCED),
                ApprovedRouteRegistry(
                    (
                        ApprovedRouteMetadata(
                            binding.route_id,
                            binding.digest,
                            binding.provider_config_digest,
                            binding.credential_reference,
                        ),
                    )
                ),
            ),
        )
        # Construction must not resolve local settings or invoke this factory.
        definitions = (
            RouteProviderDefinition(
                binding.route_id,
                binding.provider_config_digest,
                binding.credential_reference,
                lambda _binding: object(),
            ),
        )
        registry = FrozenRouteProviderRegistry(definitions)
        return FrozenRouteGoalComposition(runtime, registry, "c" * 64)

    def test_default_goal_assembly_has_no_frozen_runtime_kwargs(self) -> None:
        self.assertEqual(
            _frozen_route_runtime_kwargs(None, config_path=Path("routing.toml")),
            {},
        )

    def test_composition_wires_exact_admission_authority_and_cross_check(self) -> None:
        composition = self._composition()
        kwargs = _frozen_route_runtime_kwargs(
            composition,
            config_path=Path(
                relpath(composition._admission_runtime.config_path, Path.cwd())
            ),
        )

        self.assertEqual(set(kwargs), {
            "frozen_route_binding_resolver",
            "frozen_route_admission_resolver",
            "frozen_route_registry",
        })
        resolver = kwargs["frozen_route_binding_resolver"]
        self.assertIsInstance(resolver, LocalApprovedRouteRuntime)
        self.assertEqual(kwargs["frozen_route_admission_resolver"], resolver.admission_for)
        self.assertIsInstance(
            kwargs["frozen_route_registry"], FrozenRouteProviderRegistry
        )
        for name in (
            "config_path",
            "provider",
            "credential",
            "selection",
            "admission_for",
            "construct",
        ):
            self.assertFalse(hasattr(composition, name))

    def test_registry_public_static_validator_returns_admitted_closure_receipt(self) -> None:
        composition = self._composition()
        receipt = composition.registry_closure_receipt()
        self.assertEqual(receipt.status, "ADMITTED")
        self.assertEqual(
            receipt.validated_metadata,
            receipt.required_metadata,
        )

    def test_active_local_approval_rejects_first_run_before_registry_validation(self) -> None:
        composition = self._composition()
        write_local_routing_settings(
            composition._admission_runtime.config_path,
            LocalRoutingSettings(
                UserRoutingPolicy(UserRoutingPolicyMode.BALANCED),
                ApprovedRouteRegistry(()),
            ),
        )
        with mock.patch.object(
            composition._provider_registry,
            "validate_frozen_bindings",
            side_effect=AssertionError("registry validation must not run"),
        ):
            with self.assertRaisesRegex(
                ValueError, "DENIED_FIRST_RUN_NO_APPROVED_ROUTES"
            ):
                _frozen_route_runtime_kwargs(
                    composition,
                    config_path=composition._admission_runtime.config_path,
                )

    def test_selection_closure_rejects_missing_route_policy_and_assignment_digest(self) -> None:
        original = self._composition()
        runtime = original._admission_runtime
        binding = runtime.frozen_runtime_policy.bindings[0]
        assignment = runtime.frozen_runtime_policy.plan.assignments[0]
        original_receipt = runtime.frozen_selection_receipts[0].selection_receipt

        wrong_route_receipt = RouteSelectionReceipt(
            candidates=(RouteCandidateReceipt("other-route"),),
            selected_route_id="other-route",
            selection_reasons=(SelectionReason.HARD_CONSTRAINTS_SATISFIED,),
            policy_digest=binding.orchestration_policy_digest,
        )
        wrong_policy_receipt = RouteSelectionReceipt(
            candidates=(RouteCandidateReceipt(binding.route_id),),
            selected_route_id=binding.route_id,
            selection_reasons=(SelectionReason.HARD_CONSTRAINTS_SATISFIED,),
            policy_digest="d" * 64,
        )
        mismatched_policy = MultiRouteRuntimePolicy(
            MultiRouteJobPlan(
                runtime.frozen_runtime_policy.plan.graph_digest,
                (
                    replace(
                        assignment,
                        expected_selection_receipt_digest="e" * 64,
                    ),
                ),
                (),
                runtime.frozen_runtime_policy.plan.acting_integrator_id,
            ),
            (binding,),
        )
        malformed_runtime = replace(runtime)
        object.__setattr__(
            malformed_runtime,
            "frozen_selection_receipts",
            (object(),),
        )
        variants = (
            (
                replace(runtime, frozen_selection_receipts=None),
                "closure is required",
            ),
            (
                malformed_runtime,
                "closure is malformed",
            ),
            (
                replace(
                    runtime,
                    frozen_selection_receipts=(
                        PreFrozenSelectionReceipt(binding.digest, wrong_route_receipt),
                    ),
                ),
                "wrong route",
            ),
            (
                replace(
                    runtime,
                    frozen_selection_receipts=(
                        PreFrozenSelectionReceipt(binding.digest, wrong_policy_receipt),
                    ),
                ),
                "wrong policy",
            ),
            (
                LocalApprovedRouteRuntime(
                    runtime.config_path,
                    mismatched_policy,
                    (PreFrozenSelectionReceipt(binding.digest, original_receipt),),
                ),
                "digest mismatches",
            ),
        )
        for drifted_runtime, message in variants:
            with self.subTest(message=message):
                composition = FrozenRouteGoalComposition(
                    drifted_runtime,
                    original._provider_registry,
                    "c" * 64,
                )
                with mock.patch.object(
                    original._provider_registry,
                    "validate_frozen_bindings",
                    side_effect=AssertionError("registry validation must not run"),
                ):
                    with self.assertRaisesRegex(ValueError, message):
                        composition.require_registry_closure()

    def test_composition_rejects_wrong_dependencies_and_goal_helper_rejects_other_values(self) -> None:
        with self.assertRaises(TypeError):
            FrozenRouteGoalComposition(object(), object())
        with self.assertRaises(TypeError):
            _frozen_route_runtime_kwargs(object(), config_path=Path("routing.toml"))

    def test_shadow_coding_and_frozen_composition_fail_before_resource_assembly(self) -> None:
        composition = self._composition()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RunCommandConfig(
                goal="no dispatch",
                workspace=root,
                state_path=root / "runtime.db",
                provider_kind="openai_codex",
                base_url="",
                model="unused",
                codex_model="unused",
                codex_command="codex",
                api_key_env=None,
                request_timeout_seconds=1.0,
                permission_mode="ask",
                run_limits=make_request().limits,
            )
            with mock.patch.object(
                goal_runtime._JobRuntimeResources,
                "acquire",
                side_effect=AssertionError("resource assembly must not run"),
            ):
                with self.assertRaisesRegex(
                    ValueError, "Frozen native route composition is unavailable"
                ):
                    import asyncio

                    asyncio.run(
                        goal_runtime.run_goal(
                            config,
                            object(),
                            coding_worker=object(),
                            frozen_route_composition=composition,
                        )
                    )

    def test_finalized_company_graph_yields_guard_and_rejects_assignment_drift(self) -> None:
        request = self._company_request()
        composition = self._composition(company_request_value=request)
        guard = composition.assignment_admission_for(request)
        event = TaskAssignmentEvent(
            request.job_id,
            "task-1",
            1,
            "employee-researcher",
            "Repository Analyst",
            False,
            ("repository_analysis",),
            (),
            1,
            True,
            "fixture",
            1,
        )
        self.assertEqual(guard(event), _binding().digest)
        with self.assertRaisesRegex(ValueError, "does not match frozen"):
            guard(
                TaskAssignmentEvent(
                    request.job_id,
                    "task-1",
                    1,
                    "different-employee",
                    "Repository Analyst",
                    False,
                    ("repository_analysis",),
                    (),
                    1,
                    True,
                    "fixture",
                    1,
                )
            )

    def test_frozen_guard_rejects_graph_version_and_attempt_state_drift(self) -> None:
        request = self._company_request()
        composition = self._composition(company_request_value=request)
        guard = composition.assignment_admission_for(request)
        event = TaskAssignmentEvent(
            request.job_id,
            "task-1",
            1,
            "employee-researcher",
            "Repository Analyst",
            False,
            ("repository_analysis",),
            (),
            1,
            True,
            "fixture",
            1,
        )

        with self.assertRaisesRegex(ValueError, "does not match frozen"):
            guard(replace(event, graph_version=2))
        with self.assertRaisesRegex(ValueError, "does not match frozen"):
            guard(replace(event, attempt=2))
        self.assertEqual(guard(event), _binding().digest)

    def test_graph_drift_fails_before_kernel_callback_construction(self) -> None:
        request = self._company_request()
        composition = self._composition()
        with self.assertRaisesRegex(ValueError, "does not match the finalized Company graph"):
            composition.assignment_admission_for(request)

    def test_same_graph_cannot_replay_composition_for_another_job_or_authority(self) -> None:
        request = self._company_request()
        composition = self._composition(company_request_value=request)
        composition.assignment_admission_for(request)
        for changed_request in (
            replace(request, job_id="another-fixture-job"),
            replace(
                request,
                work_order_id="work-order-other",
                work_order_digest="d" * 64,
                work_order_authority_digest="e" * 64,
                company_revision=request.company_revision + 1,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "different Company request identity"):
                composition.assignment_admission_for(changed_request)

    def test_equal_identity_replay_and_moved_state_authority_are_rejected(self) -> None:
        request = self._company_request()
        composition = self._composition(company_request_value=request)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            composition.assignment_admission_for(
                request,
                state_path=root / "runtime.db",
            )
            with self.assertRaisesRegex(ValueError, "already been issued"):
                composition.assignment_admission_for(
                    request,
                    state_path=root / "runtime.db",
                )

            moved = self._composition(company_request_value=request)
            moved.assignment_admission_for(
                request,
                state_path=root / "runtime.db",
            )
            with self.assertRaisesRegex(ValueError, "different Company request identity"):
                moved.assignment_admission_for(
                    request,
                    state_path=root / "moved-runtime.db",
                )

    def test_divergent_config_source_rejects_before_resource_assembly(self) -> None:
        composition = self._composition()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RunCommandConfig(
                goal="no dispatch",
                workspace=root,
                state_path=root / "runtime.db",
                provider_kind="openai",
                base_url="",
                model="unused",
                codex_model="unused",
                codex_command="codex",
                api_key_env=None,
                request_timeout_seconds=1.0,
                permission_mode="deny",
                run_limits=make_request().limits,
                config_path=root / "different-routing.toml",
            )
            with mock.patch.object(
                goal_runtime._JobRuntimeResources,
                "acquire",
                side_effect=AssertionError("resource assembly must not run"),
            ):
                with self.assertRaisesRegex(ValueError, "approval source does not match"):
                    import asyncio

                    asyncio.run(
                        goal_runtime.run_goal(
                            config,
                            object(),
                            frozen_route_composition=composition,
                        )
                    )

    def test_execute_admitted_goal_passes_frozen_guard_and_default_none_to_kernel(self) -> None:
        request = self._company_request()
        composition = self._composition(company_request_value=request)
        guard = composition.assignment_admission_for(request)
        captured: list[object] = []

        class KernelProbe:
            def __init__(self, **kwargs) -> None:
                captured.append(kwargs.get("assignment_admission"))

            async def run(self, _request):
                return "completed"

        ports = GoalCompletionPorts(
            active_job_inspector=object(),
            direct_company_executor=object(),
            evidence_source=object(),
            firm_kernel=KernelProbe,
            initial_coordination_policy=object(),
            input_route=InputRoute,
            product_event=object(),
            product_event_type=object(),
            sqlite_active_job_ledger=lambda *_args, **_kwargs: object(),
            action_policy=object(),
            emit_product_event=object(),
            has_configured_external_read_capability=object(),
            company_final_report=object(),
            episode_from_runtime_ledger=object(),
            organization_outcome_metrics=object(),
            staffing_demands_from_runtime_ledger=object(),
            product_event_from_assignment=object(),
            company_work_mode=object(),
        )

        async def run(admission):
            return await execute_admitted_goal(
                firm_coordinator=object(), work_order=object(), firm_runtime_coordination=object(),
                route=InputRoute.COMPANY_GOAL, event_sink=None, workflow_priors=(),
                manager_assignment=None, replanner=object(), assignment_sink=None,
                service=object(), company_budget_authority=None, request=request,
                approval_port=None, evolution_artifact_pins=(),
                evolution_artifact_resolution=SimpleNamespace(effects=()),
                config=SimpleNamespace(company_coordination="local"), store=object(),
                job_id=request.job_id, ports=ports, assignment_admission=admission,
            )

        import asyncio

        completed, _, _ = asyncio.run(run(guard))
        self.assertEqual(completed, "completed")
        self.assertIs(captured[-1], guard)
        asyncio.run(run(None))
        self.assertIsNone(captured[-1])

    def test_conversation_route_with_frozen_composition_fails_before_resource_assembly(self) -> None:
        composition = self._composition()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RunCommandConfig(
                goal="no dispatch",
                workspace=root,
                state_path=root / "runtime.db",
                provider_kind="openai",
                base_url="",
                model="unused",
                codex_model="unused",
                codex_command="codex",
                api_key_env=None,
                request_timeout_seconds=1.0,
                permission_mode="deny",
                run_limits=make_request().limits,
            )
            with mock.patch.object(
                goal_runtime._JobRuntimeResources,
                "acquire",
                side_effect=AssertionError("resource assembly must not run"),
            ):
                with self.assertRaisesRegex(ValueError, "only for managed Company goals"):
                    import asyncio

                    asyncio.run(
                        goal_runtime.run_goal(
                            config,
                            object(),
                            route=InputRoute.CONVERSATION,
                            frozen_route_composition=composition,
                        )
                    )
