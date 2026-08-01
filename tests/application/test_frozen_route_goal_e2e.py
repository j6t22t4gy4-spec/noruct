from __future__ import annotations

import asyncio
import argparse
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from dynamic_firm.application import goal_runtime
from dynamic_firm.application.company_runtime_assembly import _load_active_roster
from dynamic_firm.application.frozen_route_goal_composition import (
    FrozenRouteContinuationBundle,
    FrozenRouteContinuationCatalog,
    FrozenRouteGoalComposition,
)
from dynamic_firm.application.goal_execution import GoalExecutionServices
from dynamic_firm.application.job_cli import run_job_command
from dynamic_firm.application.modern_terminal_job_audit import job_audit_snapshot
from dynamic_firm.application.job_continuation import ReceiptBoundContinuationService
from dynamic_firm.application.goal_runtime import run_goal
from dynamic_firm.application.local_approved_route_runtime import (
    LocalApprovedRouteRuntime,
    PreFrozenSelectionReceipt,
)
from dynamic_firm.cli import RunCommandConfig
from dynamic_firm.company.execution_route_binding import ExecutionRouteBinding
from dynamic_firm.company.controlled_benchmark_harness import (
    BenchmarkStrategy,
    ControlledBenchmarkHarness,
    ControlledScenarioEnvelope,
    DataEgressClass,
    ObservationAvailability,
    SyntheticStrategyResult,
)
from dynamic_firm.company.fallback_admission import FallbackFailureKind
from dynamic_firm.company.independent_verification_plan import (
    IndependentCallShape,
    IndependentVerificationPlan,
)
from dynamic_firm.company import CompanyStateStore
from dynamic_firm.company.work_order_portfolio import WorkOrderPortfolioStore
from dynamic_firm.company.graph_blueprint_models import (
    GraphBlueprint,
    GraphBlueprintOrigin,
    GraphBlueprintTask,
)
from dynamic_firm.company.graph_blueprint_registry import SQLiteGraphBlueprintRegistry
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
from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.kernel.models import EmployeeRecord, JobTask, PlanProposal
from dynamic_firm.kernel.mutation import content_digest, graph_structure_digest
from dynamic_firm.product.local_routing_settings import (
    LocalRoutingSettings,
    write_local_routing_settings,
)
from dynamic_firm.product import InputRoute
from dynamic_firm.providers.fake import ScriptedModelProvider
from dynamic_firm.providers.admitted_fallback import (
    AdmittedFallbackModelProvider,
    FallbackAdmissionPolicy,
)
from dynamic_firm.runtime.models import (
    CompletionEnvelope,
    ModelResponse,
    ModelStreamProgress,
    RunLimits,
)
from dynamic_firm.runtime.job_ledger import ActiveJobPartialContinuation
from dynamic_firm.runtime.ports import ModelProviderError
from dynamic_firm.runtime.store import RunStore


_GOAL = "Research the issue, then design a fix, and finally implement and test it"
_TASK_IDS = ("research", "design", "implementation")
_EMPLOYEE_IDS = (
    "employee-repository-analyst",
    "employee-company-generalist",
    "employee-implementation-specialist",
)


async def _return(value):  # type: ignore[no-untyped-def]
    return value


class _StreamingScriptedModelProvider(ScriptedModelProvider):
    """Provider-free adapter matching the foundation worker's streaming port."""

    async def complete_stream(self, request, cancellation, progress):  # type: ignore[no-untyped-def]
        response = await self.complete(request, cancellation)
        progress(ModelStreamProgress(1, len(response.content), True))
        return response


def _binding(route_id: str, *, config_digest: str) -> ExecutionRouteBinding:
    values: dict[str, object] = {
        "attempt_id": f"attempt-{route_id}",
        "route_id": route_id,
        "execution_profile_id": f"profile-{route_id}",
        "provider_config_digest": config_digest,
        "credential_reference": "FROZEN_FIXTURE_KEY",
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


def _proposal() -> PlanProposal:
    return PlanProposal(
        proposal_id="blueprint-frozen-route-goal-v1-work-order-fixture",
        goal=_GOAL,
        tasks=(
            JobTask(
                task_id="research",
                objective=f"Research {_GOAL}.",
                depends_on=(),
                required_capabilities=("repository_analysis",),
                acceptance_criteria=("Record research evidence.",),
            ),
            JobTask(
                task_id="design",
                objective=f"Design {_GOAL}.",
                depends_on=(),
                required_capabilities=("general_reasoning",),
                acceptance_criteria=("Record design evidence.",),
            ),
            JobTask(
                task_id="implementation",
                objective=f"Implement {_GOAL}.",
                depends_on=("research", "design"),
                required_capabilities=("implementation",),
                acceptance_criteria=("Record implementation evidence.",),
            ),
        ),
        final_task_id="implementation",
    )


def _blueprint() -> GraphBlueprint:
    return GraphBlueprint(
        blueprint_id="frozen-route-goal",
        version=1,
        objective_class="general",
        execution_profiles=("read_only",),
        parameters=("objective", "requested_outcome"),
        tasks=tuple(
            GraphBlueprintTask(
                task_id=task.task_id,
                objective_template=task.objective.replace(_GOAL, "{{objective}}"),
                depends_on=task.depends_on,
                required_capabilities=task.required_capabilities,
                acceptance_templates=task.acceptance_criteria,
            )
            for task in _proposal().tasks
        ),
        final_task_id="implementation",
        origin=GraphBlueprintOrigin.DRAFT,
    )


class FrozenRouteGoalE2ETests(unittest.TestCase):
    @staticmethod
    def _dependency_task_ids(provider: _StreamingScriptedModelProvider) -> tuple[str, ...]:
        """Read the provider-free prompt projection without treating it as authority."""
        for message in provider.requests[0].messages:
            if not message.content.startswith("Current task context\n"):
                continue
            payload = json.loads(message.content.removeprefix("Current task context\n"))
            dependencies = payload.get("task_dependencies", ())
            return tuple(
                str(json.loads(item["content"])["task_id"])
                for item in dependencies
                if str(item.get("content_id", "")).startswith("task-result:")
            )
        return ()

    def _config(self, root: Path) -> RunCommandConfig:
        config = RunCommandConfig(
            goal=_GOAL,
            workspace=root,
            state_path=root / "runtime.db",
            provider_kind="openai_api",
            base_url="https://unused.invalid/v1",
            model="untrusted-default-model",
            codex_model=None,
            codex_command="codex",
            api_key_env=None,
            request_timeout_seconds=1.0,
            permission_mode="read-only",
            run_limits=RunLimits(max_model_calls=8, max_wall_time_ms=30_000),
            config_path=root / "routing.toml",
        )
        self._seed_roster(config)
        return config

    @staticmethod
    def _seed_roster(config: RunCommandConfig) -> None:
        """Seed the actual Company ROSTER; route selection stays outside it."""
        with CompanyStateStore(config.state_path) as store:
            store.ensure_roster_baseline(
                (
                    EmployeeRecord(
                        _EMPLOYEE_IDS[0],
                        "Repository Analyst",
                        ("repository_analysis",),
                        model_profile=config.model,
                    ),
                    EmployeeRecord(
                        _EMPLOYEE_IDS[1],
                        "Company Generalist",
                        ("general_reasoning",),
                        model_profile=config.model,
                    ),
                    EmployeeRecord(
                        _EMPLOYEE_IDS[2],
                        "Implementation Specialist",
                        ("implementation",),
                        model_profile=config.model,
                    ),
                )
            )

    def _seed_blueprint(self, state_path: Path) -> None:
        registry = SQLiteGraphBlueprintRegistry(
            state_path.with_name(f"{state_path.stem}.graph-blueprints.db")
        )
        try:
            blueprint = registry.save(_blueprint())
            registry.pin("default", blueprint.ref)
        finally:
            registry.close()

    def _composition(
        self,
        root: Path,
        *,
        mismatch_research_employee: bool = False,
        omitted_registry_route_id: str | None = None,
        independent_mode: str | None = None,
        admitted_research_fallback: bool = False,
        blocked_initial_routes: bool = False,
    ) -> tuple[
        FrozenRouteGoalComposition,
        list[tuple[str, object]],
    ]:
        bindings = tuple(
            _binding(task_id, config_digest=letter * 64)
            for task_id, letter in zip(_TASK_IDS, ("a", "c", "d"), strict=True)
        )
        receipts = tuple(
            PreFrozenSelectionReceipt(
                binding.digest,
                RouteSelectionReceipt(
                    candidates=(RouteCandidateReceipt(binding.route_id),),
                    selected_route_id=binding.route_id,
                    selection_reasons=(SelectionReason.HARD_CONSTRAINTS_SATISFIED,),
                    policy_digest=binding.orchestration_policy_digest,
                ),
            )
            for binding in bindings
        )
        receipt_by_digest = {
            receipt.binding_digest: receipt.selection_receipt.digest for receipt in receipts
        }
        graph_digest = graph_structure_digest(graph_from_proposal(_proposal(), max_tasks=6))
        assignments = tuple(
            TaskRouteAssignment(
                task_id,
                (
                    "employee-company-generalist"
                    if mismatch_research_employee and task_id == "research"
                    else employee_id
                ),
                binding.digest,
                depends_on=next(
                    task.depends_on
                    for task in _proposal().tasks
                    if task.task_id == task_id
                ),
                final=task_id == "implementation",
                expected_selection_receipt_digest=receipt_by_digest[binding.digest],
            )
            for task_id, binding, employee_id in zip(
                _TASK_IDS, bindings, _EMPLOYEE_IDS, strict=True
            )
        )
        policy = MultiRouteRuntimePolicy(
            MultiRouteJobPlan(
                graph_digest,
                assignments,
                (
                    DependencyArtifactHandoff(
                        "research",
                        "implementation",
                        content_digest({
                            "schema": "noruct.task-dependency.v1",
                            "source_task_id": "research",
                            "target_task_id": "implementation",
                        }),
                    ),
                    DependencyArtifactHandoff(
                        "design",
                        "implementation",
                        content_digest({
                            "schema": "noruct.task-dependency.v1",
                            "source_task_id": "design",
                            "target_task_id": "implementation",
                        }),
                    ),
                ),
                _EMPLOYEE_IDS[-1],
            ),
            bindings,
        )
        settings = LocalRoutingSettings(
            UserRoutingPolicy(UserRoutingPolicyMode.BALANCED),
            ApprovedRouteRegistry(
                tuple(
                    ApprovedRouteMetadata(
                        binding.route_id,
                        binding.digest,
                        binding.provider_config_digest,
                        binding.credential_reference,
                    )
                    for binding in bindings
                )
            ),
        )
        config_path = root / "routing.toml"
        write_local_routing_settings(config_path, settings)
        created: list[tuple[str, object]] = []

        def response_for(binding: ExecutionRouteBinding) -> ModelResponse:
            return ModelResponse(
                completion=CompletionEnvelope(
                    summary=f"completed-{binding.route_id}",
                    acceptance_evidence=("fixture evidence",),
                )
            )

        def factory(binding: ExecutionRouteBinding) -> object:
            if admitted_research_fallback and binding.route_id == "research":
                primary = ScriptedModelProvider(
                    [
                        ModelProviderError(
                            "MODEL_TRANSPORT_ERROR",
                            "synthetic pre-effect transport failure",
                            retryable=True,
                        )
                    ]
                )
                backup = ScriptedModelProvider([response_for(binding)])
                provider: object = AdmittedFallbackModelProvider(
                    (("research-primary", primary), ("research-backup", backup)),
                    policy=FallbackAdmissionPolicy(
                        approved_pairs=frozenset(
                            {("research-primary", "research-backup")}
                        ),
                        failure_kinds={
                            "MODEL_TRANSPORT_ERROR": FallbackFailureKind.TRANSPORT,
                        },
                    ),
                )
            else:
                provider = _StreamingScriptedModelProvider(
                    [response_for(binding)],
                    blocked_calls=(
                        (0,)
                        if blocked_initial_routes
                        and binding.route_id in {"research", "design"}
                        else ()
                    ),
                )
            created.append((binding.route_id, provider))
            return provider

        registry = FrozenRouteProviderRegistry(
            tuple(
                RouteProviderDefinition(
                    binding.route_id,
                    binding.provider_config_digest,
                    binding.credential_reference,
                    factory,
                )
                for binding in bindings
                if binding.route_id != omitted_registry_route_id
            )
        )
        independent_plan = None
        if independent_mode is not None:
            candidate = IndependentCallShape(
                provider_route_digest=bindings[0].digest,
                model_identity_digest="1" * 64,
                context_projection_digest="2" * 64,
                source_projection_digest="3" * 64,
                tools_enabled=False,
                read_only=False,
            )
            verifier = IndependentCallShape(
                provider_route_digest=(
                    bindings[0].digest
                    if independent_mode == "clone"
                    else bindings[2].digest
                ),
                model_identity_digest=("1" if independent_mode == "clone" else "4") * 64,
                context_projection_digest=("2" if independent_mode == "clone" else "5") * 64,
                source_projection_digest=("3" if independent_mode == "clone" else "6") * 64,
                tools_enabled=False,
                read_only=True,
            )
            independent_plan = IndependentVerificationPlan(
                candidate=candidate,
                verifier=verifier,
                error_correlation=-0.2,
            )
        return (
            FrozenRouteGoalComposition(
                LocalApprovedRouteRuntime(config_path, policy, receipts),
                registry,
                _blueprint().content_digest,
                independent_verification_plan=independent_plan,
            ),
            created,
        )

    @staticmethod
    def _run_through_product_ingress(
        config: RunCommandConfig,
        default_provider: ScriptedModelProvider,
        composition: FrozenRouteGoalComposition,
        *,
        request_id: str,
        job_id: str,
    ):
        """Use the real product composition path; no run caller assembles ports."""
        services = GoalExecutionServices(
            config_for=lambda _args, _settings: config,
            roster_for=_load_active_roster,
            provider_config_for=lambda _config: object(),
            provider_factory=lambda _provider_config: default_provider,
            coding_worker_for=lambda _provider_config, _config: None,
            approval_available_for=lambda _config: False,
            runner=run_goal,
            frozen_route_composition_for=lambda _config, _roster: composition,
        )
        prepared = services.prepare(argparse.Namespace(), {})
        return asyncio.run(
            services.execute(
                prepared,
                route=InputRoute.COMPANY_GOAL,
                request_id=request_id,
                job_id=job_id,
            )
        )

    @staticmethod
    def _continuation_catalog(
        config: RunCommandConfig,
        composition: FrozenRouteGoalComposition,
    ) -> FrozenRouteContinuationCatalog:
        """Reassemble only provider-free adapters; no factory is called here."""
        policy = composition._admission_runtime.frozen_runtime_policy

        def factory(binding: ExecutionRouteBinding) -> object:
            return _StreamingScriptedModelProvider([
                ModelResponse(
                    completion=CompletionEnvelope(
                        summary=f"reassembled-{binding.route_id}",
                        acceptance_evidence=("fixture evidence",),
                    )
                )
            ])

        return FrozenRouteContinuationCatalog(
            config.config_path,
            tuple(
                RouteProviderDefinition(
                    binding.route_id,
                    binding.provider_config_digest,
                    binding.credential_reference,
                    factory,
                )
                for binding in policy.bindings
            ),
        )

    def test_plan_first_goal_uses_each_frozen_route_and_persists_admissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            self._seed_blueprint(config.state_path)
            composition, created = self._composition(root)
            default_provider = ScriptedModelProvider([])

            result = self._run_through_product_ingress(
                config,
                default_provider,
                composition,
                request_id="request-frozen-goal-e2e",
                job_id="job-frozen-goal-e2e",
            )

            self.assertEqual(result.final_task_id, "implementation")
            self.assertEqual(
                tuple(task.task_id for task in result.final_tasks), _TASK_IDS
            )
            self.assertEqual(default_provider.call_count, 0)
            self.assertEqual({route_id for route_id, _ in created}, set(_TASK_IDS))
            self.assertTrue(all(provider.call_count == 1 for _, provider in created))
            providers_by_route = dict(created)
            self.assertEqual(
                self._dependency_task_ids(providers_by_route["design"]),
                (),
            )
            self.assertEqual(
                self._dependency_task_ids(providers_by_route["implementation"]),
                ("research", "design"),
            )
            store = RunStore(config.state_path)
            try:
                runs = store.list_job_runs(result.job_id)
                dependency_receipts = store.list_job_dependency_result_receipts(result.job_id)
                self.assertEqual(
                    [str(receipt["task_id"]) for receipt in dependency_receipts],
                    sorted(_TASK_IDS),
                )
                runs_by_task = {str(run["task_id"]): run for run in runs}
                self.assertEqual(set(runs_by_task), set(_TASK_IDS))
                self.assertEqual(
                    [runs_by_task[task_id]["employee_id"] for task_id in _TASK_IDS],
                    list(_EMPLOYEE_IDS),
                )
                admissions = [
                    store.get_frozen_route_admission(
                        str(runs_by_task[task_id]["run_id"])
                    )
                    for task_id in _TASK_IDS
                ]
                self.assertTrue(all(admission is not None for admission in admissions))
                self.assertEqual(
                    [admission.binding.route_id for admission in admissions if admission],
                    list(_TASK_IDS),
                )
                self.assertEqual(
                    [admission.binding.digest for admission in admissions if admission],
                    [
                        next(
                            binding.digest
                            for binding in composition._admission_runtime.frozen_runtime_policy.bindings
                            if binding.digest
                            == next(
                                assignment.route_binding_digest
                                for assignment in composition._admission_runtime.frozen_runtime_policy.plan.assignments
                                if assignment.task_id == task_id
                            )
                        )
                        for task_id in _TASK_IDS
                    ],
                )
                self.assertEqual(
                    [admission.selection_receipt.digest for admission in admissions if admission],
                    [
                        next(
                            assignment.expected_selection_receipt_digest
                            for assignment in composition._admission_runtime.frozen_runtime_policy.plan.assignments
                            if assignment.task_id == task_id
                        )
                        for task_id in _TASK_IDS
                    ],
                )
            finally:
                store.close()
            route_surface = job_audit_snapshot(config.state_path, result.job_id)
            common = route_surface["route_operator_projections"]
            self.assertEqual(len(common), 3)
            self.assertEqual(
                {item["egress_policy_state"] for item in common},
                {"UNVERIFIED"},
            )
            self.assertEqual(
                {item["fallback_state"] for item in common},
                {"NOT_USED"},
            )
            rendered = json.dumps(common, sort_keys=True)
            self.assertNotIn("FROZEN_FIXTURE_KEY", rendered)
            self.assertNotIn("untrusted-default-model", rendered)
            cli_output = io.StringIO()
            self.assertEqual(
                run_job_command(
                    argparse.Namespace(
                        job_command="inspect",
                        job_id=result.job_id,
                        json=False,
                    ),
                    state_path=config.state_path,
                    settings={},
                    output=cli_output,
                ),
                0,
            )
            cli_text = cli_output.getvalue()
            self.assertIn("ROUTE EXECUTION · READ ONLY", cli_text)
            self.assertIn("송신=UNVERIFIED", cli_text)
            self.assertIn("대체=NOT_USED", cli_text)
            self.assertNotIn("FROZEN_FIXTURE_KEY", cli_text)
            portfolio_path = config.state_path.with_name(
                f"{config.state_path.stem}.work-orders.db"
            )
            with WorkOrderPortfolioStore(portfolio_path) as authority:
                retained_request = authority.continuation_request(result.job_id)
                persisted = authority.frozen_route_continuation_bundle(result.job_id)
                self.assertIsNotNone(persisted)
                assert persisted is not None
                bundle_json, bundle_digest = persisted
                bundle = FrozenRouteContinuationBundle.from_canonical_json(bundle_json)
                self.assertEqual(bundle.digest, bundle_digest)
                self.assertEqual(
                    bundle,
                    composition.continuation_bundle_for(
                        retained_request,
                        state_path=config.state_path,
                    ),
                )

    def test_independent_candidate_and_verifier_are_dispatched_no_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            self._seed_blueprint(config.state_path)
            composition, created = self._composition(root, independent_mode="pair")

            result = self._run_through_product_ingress(
                config,
                ScriptedModelProvider([]),
                composition,
                request_id="request-frozen-goal-independent-pair",
                job_id="job-frozen-goal-independent-pair",
            )

            self.assertEqual(result.final_task_id, "implementation")
            providers_by_route = dict(created)
            self.assertEqual(providers_by_route["research"].requests[0].tools, ())
            self.assertEqual(providers_by_route["implementation"].requests[0].tools, ())
            self.assertTrue(providers_by_route["design"].requests[0].tools)

    def test_clone_independent_plan_rejects_before_resource_or_provider_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            self._seed_blueprint(config.state_path)
            composition, created = self._composition(root, independent_mode="clone")
            default_provider = ScriptedModelProvider([])

            with mock.patch.object(
                goal_runtime._JobRuntimeResources,
                "acquire",
                side_effect=AssertionError("resource assembly must not run"),
            ):
                with self.assertRaisesRegex(
                    ValueError, "not effectively independent"
                ):
                    asyncio.run(
                        run_goal(
                            config,
                            default_provider,
                            frozen_route_composition=composition,
                            request_id="request-frozen-goal-independent-clone",
                            job_id="job-frozen-goal-independent-clone",
                        )
                    )

            self.assertEqual(default_provider.call_count, 0)
            self.assertEqual(created, [])

    def test_admitted_fallback_uses_only_frozen_pre_effect_pair_and_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            self._seed_blueprint(config.state_path)
            composition, created = self._composition(
                root,
                admitted_research_fallback=True,
            )

            result = self._run_through_product_ingress(
                config,
                ScriptedModelProvider([]),
                composition,
                request_id="request-frozen-goal-admitted-fallback",
                job_id="job-frozen-goal-admitted-fallback",
            )

            research_provider = dict(created)["research"]
            self.assertIsInstance(research_provider, AdmittedFallbackModelProvider)
            primary = research_provider.providers[0][1]
            backup = research_provider.providers[1][1]
            self.assertEqual(primary.call_count, 1)
            self.assertEqual(backup.call_count, 1)
            store = RunStore(config.state_path)
            try:
                research_run = next(
                    run for run in store.list_job_runs(result.job_id)
                    if run["task_id"] == "research"
                )
                receipts = store.list_model_invocation_receipts(
                    str(research_run["run_id"])
                )
            finally:
                store.close()
            child_receipts = [
                receipt for receipt in receipts if receipt.fanout_parent_id is not None
            ]
            self.assertEqual(len(child_receipts), 2)
            self.assertEqual(
                {receipt.terminal_status.value for receipt in child_receipts},
                {"FAILED", "SUCCEEDED"},
            )
            self.assertEqual(
                len([receipt for receipt in receipts if receipt.fanout_parent_id is None]),
                1,
            )

    def test_provider_free_e4_dispatch_feeds_heterogeneous_controlled_benchmark_arm(self) -> None:
        """One actual E04 route run can enter the no-winner comparison matrix.

        The remaining strategy rows deliberately stay provider-free controls:
        no synthetic quality row is presented as a real quality observation,
        and no aggregate winner is emitted by the harness.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            self._seed_blueprint(config.state_path)
            composition, _ = self._composition(root)
            result = self._run_through_product_ingress(
                config,
                ScriptedModelProvider([]),
                composition,
                request_id="request-frozen-goal-benchmark-heterogeneous",
                job_id="job-frozen-goal-benchmark-heterogeneous",
            )
            store = RunStore(config.state_path)
            try:
                receipts = tuple(
                    receipt
                    for run in store.list_job_runs(result.job_id)
                    for receipt in store.list_model_invocation_receipts(str(run["run_id"]))
                    if receipt.fanout_parent_id is None
                )
            finally:
                store.close()
            self.assertEqual(len(receipts), 3)
            scenario = ControlledScenarioEnvelope(
                scenario_id="provider-free-e4-control",
                task_digest=graph_structure_digest(graph_from_proposal(_proposal(), max_tasks=6)),
                tool_envelope_digest=content_digest({"effect": "READ", "tools": ()}),
                context_envelope_digest=content_digest({"fixture": "provider-free-e4"}),
                resource_envelope_digest=(
                    composition._admission_runtime.frozen_runtime_policy.summary_digest
                ),
            )
            observed_latency = sum(item.latency_ms for item in receipts)
            observed = SyntheticStrategyResult(
                strategy=BenchmarkStrategy.HETEROGENEOUS_MULTI_PROVIDER,
                envelope=scenario,
                # This local execution proves terminal routing, not quality.
                quality=0.0,
                complete_failure=result.status.value != "SUCCEEDED",
                cost_availability=ObservationAvailability.UNAVAILABLE,
                cost_usd=None,
                latency_availability=ObservationAvailability.AVAILABLE,
                latency_ms=observed_latency,
                error_correlation=0.0,
                data_egress_class=DataEgressClass.INTERNAL,
                human_review_minutes=0.0,
            )
            controls = tuple(
                SyntheticStrategyResult(
                    strategy=strategy,
                    envelope=scenario,
                    quality=0.0,
                    complete_failure=False,
                    cost_availability=ObservationAvailability.UNAVAILABLE,
                    cost_usd=None,
                    latency_availability=ObservationAvailability.UNAVAILABLE,
                    latency_ms=None,
                    error_correlation=0.0,
                    data_egress_class=DataEgressClass.INTERNAL,
                    human_review_minutes=0.0,
                )
                for strategy in (
                    BenchmarkStrategy.STRONG_SOLO,
                    BenchmarkStrategy.SAME_MODEL_BEST_OF_N,
                    BenchmarkStrategy.MANAGER_LED,
                )
            )
            matrix = ControlledBenchmarkHarness(scenario).compare((*controls, observed))
            heterogeneous = next(
                row for row in matrix.rows
                if row.strategy is BenchmarkStrategy.HETEROGENEOUS_MULTI_PROVIDER
            )
            self.assertEqual(heterogeneous.latency_ms, observed_latency)
            self.assertEqual(heterogeneous.cost_availability, ObservationAvailability.UNAVAILABLE)
            self.assertNotIn("winner", matrix.canonical_payload())
            self.assertNotIn("rank", matrix.canonical_json())

    def test_retained_frozen_bundle_requires_exact_continuation_composition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            self._seed_blueprint(config.state_path)
            composition, _ = self._composition(root)
            result = self._run_through_product_ingress(
                config,
                ScriptedModelProvider([]),
                composition,
                request_id="request-frozen-goal-continuation-bundle",
                job_id="job-frozen-goal-continuation-bundle",
            )
            catalog = self._continuation_catalog(config, composition)
            portfolio_path = config.state_path.with_name(
                f"{config.state_path.stem}.work-orders.db"
            )

            class Inspector:
                def authorize_partial_read_only_continuation(self, *_args, **_kwargs):
                    return ActiveJobPartialContinuation(
                        job_id=result.job_id,
                        request_id="request-frozen-goal-continuation-bundle",
                        work_order_id="work-order-frozen-goal-continuation-bundle",
                        work_order_digest="a" * 64,
                        graph_digest="b" * 64,
                        completed_task_ids=("research",),
                        completed_run_ids=("run-research",),
                        completed_results_digest="c" * 64,
                        required_checks=(),
                    )

            with WorkOrderPortfolioStore(portfolio_path) as authority:
                with self.assertRaisesRegex(
                    ValueError, "requires an exact reassembled composition"
                ):
                    asyncio.run(
                        ReceiptBoundContinuationService(
                            work_orders=authority,
                            inspector=Inspector(),
                            continue_partial=lambda _request, _session: self.fail("must not dispatch"),
                        ).resume_partial_read_only_job(result.job_id)
                    )

                outcome = asyncio.run(
                    ReceiptBoundContinuationService(
                        work_orders=authority,
                        inspector=Inspector(),
                        continue_partial=lambda _request, _session: _return(result),
                        frozen_route_catalog=catalog,
                        state_path=config.state_path,
                    ).resume_partial_read_only_job(result.job_id)
                )
            self.assertEqual(outcome.admission.job_id, result.job_id)
            self.assertEqual(outcome.result, result)

    def test_continuation_catalog_rejects_tampered_unstarted_route_before_factory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            self._seed_blueprint(config.state_path)
            composition, _ = self._composition(root)
            result = self._run_through_product_ingress(
                config,
                ScriptedModelProvider([]),
                composition,
                request_id="request-frozen-goal-catalog-tamper",
                job_id="job-frozen-goal-catalog-tamper",
            )
            portfolio_path = config.state_path.with_name(
                f"{config.state_path.stem}.work-orders.db"
            )
            with WorkOrderPortfolioStore(portfolio_path) as authority:
                persisted = authority.frozen_route_continuation_bundle(result.job_id)
            assert persisted is not None
            bundle = FrozenRouteContinuationBundle.from_canonical_json(persisted[0])
            policy_payload = json.loads(bundle.runtime_policy_json)
            policy_payload["bindings"][0]["route_id"] = "tampered-route"
            tampered = replace(
                bundle,
                runtime_policy_json=json.dumps(
                    policy_payload, sort_keys=True, separators=(",", ":")
                ),
            )
            catalog = self._continuation_catalog(config, composition)

            with self.assertRaisesRegex(ValueError, "runtime policy"):
                catalog.reassemble(tampered)

    def test_deadline_cancels_all_ready_frozen_routes_with_terminal_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(
                self._config(root),
                # The deadline must exercise cancellation after both READY
                # routes enter provider dispatch.  A sub-second whole-Job
                # budget races legitimate pre-dispatch validation on slower
                # CI hosts and instead tests the separate fail-before-dispatch
                # contract.
                run_limits=RunLimits(max_model_calls=8, max_wall_time_ms=5_000),
            )
            self._seed_blueprint(config.state_path)
            composition, created = self._composition(
                root,
                blocked_initial_routes=True,
            )

            result = self._run_through_product_ingress(
                config,
                ScriptedModelProvider([]),
                composition,
                request_id="request-frozen-goal-deadline-cancel",
                job_id="job-frozen-goal-deadline-cancel",
            )

            self.assertIn(result.status.value, {"BUDGET_EXHAUSTED", "CANCELLED"})
            self.assertEqual(
                {route_id for route_id, provider in created if provider.call_count},
                {"research", "design"},
            )
            store = RunStore(config.state_path)
            try:
                for run in store.list_job_runs(result.job_id):
                    if run["task_id"] not in {"research", "design"}:
                        continue
                    receipts = store.list_model_invocation_receipts(str(run["run_id"]))
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(receipts[0].terminal_status.value, "INDETERMINATE")
                    self.assertEqual(receipts[0].safe_error_code, "RUN_CANCELLED")
            finally:
                store.close()

    def test_unseeded_planner_rejects_before_default_or_route_provider_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            composition, created = self._composition(root)
            default_provider = ScriptedModelProvider([])

            with self.assertRaisesRegex(
                ValueError,
                "requires its exact preplanned local Blueprint",
            ):
                asyncio.run(
                    run_goal(
                        config,
                        default_provider,
                        frozen_route_composition=composition,
                        request_id="request-frozen-goal-unseeded",
                        job_id="job-frozen-goal-unseeded",
                    )
                )

            self.assertEqual(default_provider.call_count, 0)
            self.assertEqual(created, [])

    def test_missing_selection_closure_rejects_before_resource_or_provider_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            composition, created = self._composition(root)
            object.__setattr__(
                composition._admission_runtime,
                "frozen_selection_receipts",
                None,
            )
            default_provider = ScriptedModelProvider([])

            with mock.patch.object(
                goal_runtime._JobRuntimeResources,
                "acquire",
                side_effect=AssertionError("resource assembly must not run"),
            ):
                with self.assertRaisesRegex(ValueError, "closure is required"):
                    asyncio.run(
                        run_goal(
                            config,
                            default_provider,
                            frozen_route_composition=composition,
                            request_id="request-frozen-goal-missing-selection",
                            job_id="job-frozen-goal-missing-selection",
                        )
                    )

            self.assertEqual(default_provider.call_count, 0)
            self.assertEqual(created, [])

    def test_config_approval_mutation_rejects_before_resources_or_provider_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            composition, created = self._composition(root)
            write_local_routing_settings(
                config.config_path,
                LocalRoutingSettings(
                    UserRoutingPolicy(UserRoutingPolicyMode.BALANCED),
                    ApprovedRouteRegistry(()),
                ),
            )
            default_provider = ScriptedModelProvider([])

            with mock.patch.object(
                goal_runtime._JobRuntimeResources,
                "acquire",
                side_effect=AssertionError("resource assembly must not run"),
            ):
                with self.assertRaisesRegex(
                    ValueError, "DENIED_FIRST_RUN_NO_APPROVED_ROUTES"
                ):
                    asyncio.run(
                        run_goal(
                            config,
                            default_provider,
                            frozen_route_composition=composition,
                            request_id="request-frozen-goal-approval-drift",
                            job_id="job-frozen-goal-approval-drift",
                        )
                    )

            self.assertEqual(default_provider.call_count, 0)
            self.assertEqual(created, [])

    def test_missing_second_route_registry_closure_rejects_before_any_factory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            self._seed_blueprint(config.state_path)
            composition, created = self._composition(
                root,
                omitted_registry_route_id="design",
            )
            default_provider = ScriptedModelProvider([])

            with self.assertRaisesRegex(
                ValueError, "REGISTRY_CLOSURE_VALIDATOR_REJECTED"
            ):
                asyncio.run(
                    run_goal(
                        config,
                        default_provider,
                        frozen_route_composition=composition,
                        request_id="request-frozen-goal-missing-task-2-closure",
                        job_id="job-frozen-goal-missing-task-2-closure",
                    )
                )

            self.assertEqual(default_provider.call_count, 0)
            self.assertEqual(created, [])

    def test_blueprint_pin_drift_rejects_before_default_or_route_provider_work(self) -> None:
        """The second planning lookup cannot downgrade frozen dispatch to planning."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            self._seed_blueprint(config.state_path)
            composition, created = self._composition(root)
            default_provider = ScriptedModelProvider([])
            original_resolve = goal_runtime.cli.ManagerProposalAdapter.resolve_initial_blueprint
            resolution_count = 0

            def resolve_then_clear_pin(adapter, *args, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal resolution_count
                resolution_count += 1
                resolution = original_resolve(adapter, *args, **kwargs)
                if resolution_count == 1:
                    # The first lookup is the frozen precheck.  Simulate an
                    # intervening local pin change before planning re-reads
                    # the same registry.
                    adapter._graph_blueprints.clear_pin("default")
                return resolution

            with mock.patch.object(
                goal_runtime.cli.ManagerProposalAdapter,
                "resolve_initial_blueprint",
                autospec=True,
                side_effect=resolve_then_clear_pin,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "Blueprint changed before planning",
                ):
                    asyncio.run(
                        run_goal(
                            config,
                            default_provider,
                            frozen_route_composition=composition,
                            request_id="request-frozen-goal-pin-drift",
                            job_id="job-frozen-goal-pin-drift",
                        )
                    )

            self.assertEqual(resolution_count, 2)
            self.assertEqual(default_provider.call_count, 0)
            self.assertEqual(created, [])

    def test_assignment_mismatch_stops_before_any_route_factory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            self._seed_blueprint(config.state_path)
            composition, created = self._composition(
                root, mismatch_research_employee=True
            )
            default_provider = ScriptedModelProvider([])

            result = self._run_through_product_ingress(
                config,
                default_provider,
                composition,
                request_id="request-frozen-goal-mismatch",
                job_id="job-frozen-goal-mismatch",
            )

            self.assertEqual(result.status.value, "FAILED")
            self.assertEqual(
                result.failure_reason,
                "Frozen route admission rejected task dispatch.",
            )
            self.assertEqual(created, [])
            self.assertEqual(default_provider.call_count, 0)
