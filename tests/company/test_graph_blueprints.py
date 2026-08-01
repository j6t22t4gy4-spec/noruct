from __future__ import annotations

import unittest
from types import SimpleNamespace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from dynamic_firm.company import (
    AuthoritySnapshotIdentity,
    GraphBlueprint,
    GraphBlueprintExecutionReplica,
    GraphBlueprintControlService,
    GraphBlueprintOrigin,
    GraphBlueprintRegistry,
    GraphBlueprintTask,
    GraphMutationPolicy,
    GraphRevision,
    BlueprintRevisionStatus,
    GraphUserConstraints,
    SQLiteGraphBlueprintRegistry,
    WorkOrderBudgetSnapshot,
    bind_blueprint,
    graph_run_record,
    graph_run_record_from_active_job,
    preview_binding,
    normalize_work_order,
)
from dynamic_firm.kernel.graph import graph_from_proposal
from dynamic_firm.kernel.models import (
    EmployeeRecord,
    ExecutionReplicaAggregation,
    ExecutionReplicaStrategy,
    JobLimits,
)


def work_order():
    return normalize_work_order(
        "Analyze the release risk.",
        work_order_id="release-risk",
        authority_snapshot=AuthoritySnapshotIdentity(
            company_id="company-local",
            company_revision=1,
            roster_revision=1,
            playbook_revision=1,
            action_policy_digest="a" * 64,
        ),
        budget_snapshot=WorkOrderBudgetSnapshot(
            max_model_calls=8,
            max_tool_calls=8,
            max_cost_usd=2.0,
            max_wall_time_ms=30_000,
        ),
        requested_at=datetime(2026, 7, 26, tzinfo=UTC),
    )


def blueprint():
    return GraphBlueprint(
        blueprint_id="release-review",
        version=1,
        objective_class="general",
        execution_profiles=("read_only",),
        parameters=("objective", "requested_outcome"),
        tasks=(
            GraphBlueprintTask(
                task_id="analysis",
                objective_template="Analyze {{objective}}",
                depends_on=(),
                required_capabilities=("analysis",),
                acceptance_templates=("Risk evidence for {{requested_outcome}}",),
            ),
            GraphBlueprintTask(
                task_id="final",
                objective_template="Integrate {{objective}}",
                depends_on=("analysis",),
                required_capabilities=("analysis",),
                acceptance_templates=("A concise decision brief",),
            ),
        ),
        final_task_id="final",
    )


def replica_blueprint():
    common = {
        "strategy": ExecutionReplicaStrategy.PARTITION,
        "aggregation_task_id": "final",
        "aggregation": ExecutionReplicaAggregation.JOIN,
        "marginal_value_reason_template": (
            "The two non-overlapping release surfaces can reduce elapsed time for {{objective}}."
        ),
    }
    return GraphBlueprint(
        blueprint_id="partitioned-release-review",
        version=1,
        objective_class="general",
        execution_profiles=("read_only",),
        parameters=("objective", "requested_outcome"),
        tasks=(
            GraphBlueprintTask(
                task_id="runtime",
                objective_template="Inspect runtime risk for {{objective}}",
                depends_on=(),
                required_capabilities=("analysis",),
                acceptance_templates=("Runtime evidence",),
                execution_replica=GraphBlueprintExecutionReplica(
                    group_id="release-surfaces",
                    replica_id="runtime",
                    scope_template="runtime surface for {{objective}}",
                    **common,
                ),
            ),
            GraphBlueprintTask(
                task_id="delivery",
                objective_template="Inspect delivery risk for {{objective}}",
                depends_on=(),
                required_capabilities=("analysis",),
                acceptance_templates=("Delivery evidence",),
                execution_replica=GraphBlueprintExecutionReplica(
                    group_id="release-surfaces",
                    replica_id="delivery",
                    scope_template="delivery surface for {{objective}}",
                    **common,
                ),
            ),
            GraphBlueprintTask(
                task_id="final",
                objective_template="Join release evidence for {{objective}}",
                depends_on=("runtime", "delivery"),
                required_capabilities=("analysis",),
                acceptance_templates=("One decision for {{requested_outcome}}",),
            ),
        ),
        final_task_id="final",
    )


class GraphBlueprintTests(unittest.TestCase):
    def test_constraints_reject_non_finite_cost_ceiling(self) -> None:
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite non-negative"):
                    GraphUserConstraints(max_cost_usd=value)

    def test_blueprint_replica_binds_renders_and_previews_as_job_local_runs(self) -> None:
        source = replica_blueprint()
        payload = source.canonical_payload()
        registry = GraphBlueprintRegistry()
        parsed = GraphBlueprintControlService.parse_payload(payload)
        registry.save(parsed)
        binding = bind_blueprint(
            parsed,
            work_order=work_order(),
            limits=JobLimits(max_tasks=4, max_concurrency=3),
        )
        preview = preview_binding(
            binding,
            work_order=work_order(),
            roster=(
                EmployeeRecord(
                    employee_id="analyst",
                    role="Analyst",
                    capabilities=("analysis",),
                ),
            ),
            limits=JobLimits(max_tasks=4, max_concurrency=3),
        )

        replicas = tuple(
            task.execution_replica
            for task in binding.proposal.tasks
            if task.execution_replica is not None
        )
        self.assertEqual(parsed.content_digest, source.content_digest)
        self.assertEqual(len(replicas), 2)
        self.assertIn("Analyze the release risk", replicas[0].scope)
        self.assertEqual(preview.execution_replica_count, 2)
        self.assertEqual(
            preview.execution_replica_group_ids,
            ("release-surfaces",),
        )
        self.assertEqual(
            preview.tasks[0].execution_replica_strategy,
            ExecutionReplicaStrategy.PARTITION.value,
        )

    def test_blueprint_replica_rejects_incomplete_group_before_save(self) -> None:
        with self.assertRaisesRegex(ValueError, "must contain 2 to 4 tasks"):
            GraphBlueprint(
                blueprint_id="invalid-replica",
                version=1,
                objective_class="general",
                execution_profiles=("read_only",),
                parameters=("objective",),
                tasks=(
                    GraphBlueprintTask(
                        task_id="only",
                        objective_template="Inspect {{objective}}",
                        depends_on=(),
                        required_capabilities=("analysis",),
                        acceptance_templates=("Evidence",),
                        execution_replica=GraphBlueprintExecutionReplica(
                            group_id="invalid-group",
                            replica_id="only",
                            strategy=ExecutionReplicaStrategy.PARTITION,
                            scope_template="only scope",
                            aggregation_task_id="final",
                            aggregation=ExecutionReplicaAggregation.JOIN,
                            marginal_value_reason_template="Parallel value hypothesis",
                        ),
                    ),
                    GraphBlueprintTask(
                        task_id="final",
                        objective_template="Integrate {{objective}}",
                        depends_on=("only",),
                        required_capabilities=("analysis",),
                        acceptance_templates=("Result",),
                    ),
                ),
                final_task_id="final",
            )

    def test_bind_uses_only_declared_parameters_and_existing_graph_validation(self) -> None:
        binding = bind_blueprint(
            blueprint(),
            work_order=work_order(),
            limits=JobLimits(max_tasks=3),
            constraints=GraphUserConstraints(
                max_cost_usd=1.5,
                mutation_policy=GraphMutationPolicy.PROPOSE,
            ),
        )

        self.assertEqual(binding.proposal.final_task_id, "final")
        self.assertIn("Analyze the release risk.", binding.proposal.tasks[0].objective)
        self.assertEqual(binding.constraints.mutation_policy, GraphMutationPolicy.PROPOSE)
        self.assertEqual(len(binding.content_digest), 64)

        with self.assertRaisesRegex(ValueError, "undeclared parameters"):
            bind_blueprint(
                blueprint(),
                work_order=work_order(),
                parameters={"secret": "no"},
            )
        with self.assertRaisesRegex(ValueError, "hard cap"):
            bind_blueprint(
                blueprint(),
                work_order=work_order(),
                constraints=GraphUserConstraints(max_cost_usd=3.0),
            )

    def test_registry_preserves_exact_versions_and_pinned_first_retrieval(self) -> None:
        registry = GraphBlueprintRegistry()
        saved = registry.save(blueprint())
        registry.pin("default", saved.ref)
        fork = registry.fork(saved.ref, blueprint_id="release-review-local")

        candidates = registry.compatible(
            objective_class="release",
            execution_profile="read_only",
            available_capabilities=("analysis",),
            pin_slot="default",
        )

        self.assertEqual(candidates[0].ref, saved.ref)
        self.assertEqual(fork.origin, GraphBlueprintOrigin.USER_FORK)
        self.assertEqual(fork.parent_ref, saved.ref)
        with self.assertRaisesRegex(ValueError, "cannot be overwritten"):
            registry.save(
                GraphBlueprint(
                    blueprint_id="release-review",
                    version=1,
                    objective_class="general",
                    execution_profiles=("read_only",),
                    parameters=("objective",),
                    tasks=(
                        GraphBlueprintTask(
                            task_id="final",
                            objective_template="Different {{objective}}",
                            depends_on=(),
                            required_capabilities=("analysis",),
                            acceptance_templates=("done",),
                        ),
                    ),
                    final_task_id="final",
                )
            )

    def test_preview_is_read_only_and_exposes_budget_mode_and_constraint_warning(self) -> None:
        bound = bind_blueprint(
            blueprint(),
            work_order=work_order(),
            constraints=GraphUserConstraints(
                require_independent_review=True,
                max_concurrency=1,
                mutation_policy=GraphMutationPolicy.LOCKED,
                pinned_employee_ids=("missing",),
            ),
        )
        preview = preview_binding(
            bound,
            work_order=work_order(),
            roster=(
                EmployeeRecord(
                    employee_id="analyst",
                    role="Analyst",
                    capabilities=("analysis",),
                ),
            ),
            limits=JobLimits(max_tasks=3, max_concurrency=3),
        )

        # Multiple graph nodes assigned to one unchanged analyst remain a
        # managed SOLO shape, not a role-labelled team.
        self.assertEqual(preview.work_mode, "SOLO_JOB")
        self.assertEqual(preview.distinct_staffing_profile_count, 1)
        self.assertEqual(preview.staffing_difference_dimensions, ())
        self.assertEqual(preview.mutation_policy, GraphMutationPolicy.LOCKED)
        self.assertEqual(preview.hard_cap_cost_usd, 2.0)
        self.assertIn("Pinned Employees are unavailable", preview.constraint_warnings[0])

    def test_run_record_is_append_only_and_chains_revisions(self) -> None:
        binding = bind_blueprint(blueprint(), work_order=work_order())
        graph = graph_from_proposal(binding.proposal, max_tasks=3)
        record = graph_run_record(
            job_id="job-release-risk",
            work_order=work_order(),
            graph=graph,
            blueprint_ref=binding.blueprint_ref,
        )
        revision = GraphRevision(
            sequence=1,
            previous_graph_digest=record.initial_graph_digest,
            next_graph_digest="b" * 64,
            operation="INSERT",
            proposer="manager",
            trigger_evidence=("CAPABILITY_MISSING",),
            budget_delta=0.25,
            approval_policy=GraphMutationPolicy.BOUNDED_AUTO,
        )
        appended = record.append(revision)

        self.assertEqual(len(record.revisions), 0)
        self.assertEqual(len(appended.revisions), 1)
        with self.assertRaisesRegex(ValueError, "does not continue"):
            appended.append(
                GraphRevision(
                    sequence=2,
                    previous_graph_digest="c" * 64,
                    next_graph_digest="d" * 64,
                    operation="RETRY",
                    proposer="manager",
                    trigger_evidence=("VALIDATION_FAILED",),
                    budget_delta=0.0,
                    approval_policy=GraphMutationPolicy.PROPOSE,
                )
            )

    def test_run_record_projects_a_replay_verified_active_job_chain(self) -> None:
        initial = "a" * 64
        inspection = SimpleNamespace(
            job_id="job-release-risk",
            work_order_digest=work_order().content_digest,
            initial_graph_digest=initial,
            replay_matches=True,
            graph_blueprint_id="",
            graph_blueprint_version=0,
            graph_blueprint_digest="",
            graph_mutation_policy="BOUNDED_AUTO",
            terminal={"status": "SUCCEEDED"},
            graph_patches=(
                {
                    "sequence": 1,
                    "before_graph_digest": initial,
                    "after_graph_digest": "b" * 64,
                    "mutation_lease": {
                        "model_calls": 2,
                        "tool_calls": 3,
                        "cost_usd": 0.25,
                    },
                    "expected_impact": "CAPABILITY_COVERAGE",
                    "validation_receipt": "KERNEL_GRAPH_AND_LEASE_VALIDATED",
                    "patch": {
                        "patch_id": "insert-compliance",
                        "semantic_operation": "INSERT",
                        "trigger_task_id": "analysis",
                    },
                },
            ),
        )

        record = graph_run_record_from_active_job(inspection)

        self.assertEqual(record.initial_graph_digest, initial)
        self.assertEqual(len(record.revisions), 1)
        self.assertEqual(record.revisions[0].next_graph_digest, "b" * 64)
        self.assertEqual(record.revisions[0].budget_delta, 0.25)
        self.assertEqual(record.revisions[0].approval_policy, GraphMutationPolicy.BOUNDED_AUTO)
        self.assertEqual(record.revisions[0].expected_impact.value, "CAPABILITY_COVERAGE")
        self.assertEqual(
            record.revisions[0].validation_receipt.value,
            "KERNEL_GRAPH_AND_LEASE_VALIDATED",
        )
        self.assertEqual(record.revisions[0].observed_terminal_outcome.value, "JOB_SUCCEEDED")
        inspection.replay_matches = False
        with self.assertRaisesRegex(ValueError, "replay-verified"):
            graph_run_record_from_active_job(inspection)

    def test_sqlite_registry_persists_exact_blueprint_and_pin(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "blueprints.sqlite3"
            first = SQLiteGraphBlueprintRegistry(path)
            saved = first.save(blueprint())
            first.pin("default", saved.ref)
            first.close()

            second = SQLiteGraphBlueprintRegistry(path)
            self.assertEqual(second.pinned("default"), saved.ref)
            self.assertEqual(second.get(saved.ref).content_digest, saved.content_digest)
            self.assertEqual(len(second.list()), 1)
            second.close()

    def test_generated_blueprint_save_fork_revise_pin_and_next_order_reuse_e2e(
        self,
    ) -> None:
        """One local draft evolves additively and reopens at its exact revision."""

        with TemporaryDirectory() as directory:
            path = Path(directory) / "blueprints.sqlite3"
            registry = SQLiteGraphBlueprintRegistry(path)
            control = GraphBlueprintControlService(registry)
            generated = control.save(blueprint())
            forked = control.fork(
                generated.ref,
                blueprint_id="release-review-local",
            )
            revised_candidate = GraphBlueprint(
                blueprint_id=forked.blueprint_id,
                version=2,
                objective_class=forked.objective_class,
                execution_profiles=forked.execution_profiles,
                parameters=forked.parameters,
                tasks=(
                    GraphBlueprintTask(
                        task_id="analysis",
                        objective_template=(
                            "Analyze {{objective}} with independently cited evidence"
                        ),
                        depends_on=(),
                        required_capabilities=("analysis",),
                        acceptance_templates=(
                            "Risk evidence for {{requested_outcome}}",
                        ),
                    ),
                    forked.tasks[1],
                ),
                final_task_id=forked.final_task_id,
                origin=GraphBlueprintOrigin.USER_REVISION,
                parent_ref=forked.ref,
            )
            revised, receipt = control.revise(
                forked.ref,
                revised_candidate,
                rationale="Require independently cited analysis evidence.",
            )
            selected = control.select(revised.ref)
            registry.close()

            next_order = normalize_work_order(
                "Assess the next release candidate.",
                work_order_id="release-risk-next",
                requested_outcome="A cited release decision.",
                authority_snapshot=AuthoritySnapshotIdentity(
                    company_id="company-local",
                    company_revision=1,
                    roster_revision=1,
                    playbook_revision=1,
                    action_policy_digest="a" * 64,
                ),
                budget_snapshot=WorkOrderBudgetSnapshot(
                    max_model_calls=8,
                    max_tool_calls=8,
                    max_cost_usd=2.0,
                    max_wall_time_ms=30_000,
                ),
                requested_at=datetime(2026, 7, 27, tzinfo=UTC),
            )
            reopened = SQLiteGraphBlueprintRegistry(path)
            restored_control = GraphBlueprintControlService(reopened)
            restored_selection = restored_control.selection()
            restored_revision = restored_control.revision(
                revised.blueprint_id,
                revised.version,
            )
            binding = bind_blueprint(
                restored_revision,
                work_order=next_order,
                limits=JobLimits(max_tasks=3, max_concurrency=2),
            )
            reopened.close()

        self.assertEqual(receipt.status, BlueprintRevisionStatus.ACCEPTED)
        self.assertEqual(selected.blueprint_ref, revised.ref)
        self.assertEqual(restored_selection.blueprint_ref, revised.ref)
        self.assertEqual(restored_revision.ref, revised.ref)
        self.assertEqual(restored_revision.parent_ref, forked.ref)
        self.assertEqual(binding.blueprint_ref, revised.ref)
        self.assertEqual(binding.blueprint_ref.content_digest, revised.content_digest)
        self.assertEqual(
            dict(binding.parameters),
            {
                "objective": next_order.objective,
                "requested_outcome": next_order.requested_outcome,
            },
        )
        self.assertEqual(binding.proposal.tasks[0].required_capabilities, ("analysis",))
        self.assertIn(
            "A cited release decision.",
            binding.proposal.tasks[0].acceptance_criteria[0],
        )

    def test_surface_neutral_control_persists_selection_and_preview_contract(self) -> None:
        constraints = GraphUserConstraints(
            pinned_employee_ids=("analyst",),
            max_concurrency=1,
            mutation_policy=GraphMutationPolicy.PROPOSE,
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "blueprints.sqlite3"
            registry = SQLiteGraphBlueprintRegistry(path)
            control = GraphBlueprintControlService(registry)
            saved = control.save(blueprint())
            selection = control.select(saved.ref, constraints=constraints)
            preview = control.preview(
                ref=saved.ref,
                work_order=work_order(),
                roster=(
                    EmployeeRecord(
                        employee_id="analyst",
                        role="Analyst",
                        capabilities=("analysis",),
                    ),
                ),
                limits=JobLimits(max_tasks=3, max_concurrency=3),
            )
            registry.close()

            reopened = SQLiteGraphBlueprintRegistry(path)
            restored = GraphBlueprintControlService(reopened).catalog()
            reopened.close()

        self.assertEqual(selection.blueprint_ref, saved.ref)
        self.assertEqual(preview.mutation_policy, GraphMutationPolicy.PROPOSE)
        self.assertEqual(restored.selection.blueprint_ref, saved.ref)
        self.assertEqual(restored.selection.constraints, constraints)
        self.assertEqual(restored.blueprints[0].ref, saved.ref)

    def test_user_control_cannot_register_a_verified_playbook(self) -> None:
        registry = GraphBlueprintRegistry()
        control = GraphBlueprintControlService(registry)
        verified = GraphBlueprint(
            blueprint_id="company-qualified-only",
            version=1,
            objective_class="general",
            execution_profiles=("read_only",),
            parameters=("objective",),
            tasks=(
                GraphBlueprintTask(
                    task_id="final",
                    objective_template="Complete {{objective}}",
                    depends_on=(),
                    required_capabilities=("analysis",),
                    acceptance_templates=("A result",),
                ),
            ),
            final_task_id="final",
            origin=GraphBlueprintOrigin.VERIFIED_PLAYBOOK,
        )

        with self.assertRaisesRegex(ValueError, "qualification lifecycle"):
            control.save(verified)

    def test_user_revision_is_immutable_and_persists_an_accepted_receipt(self) -> None:
        with TemporaryDirectory() as directory:
            registry = SQLiteGraphBlueprintRegistry(Path(directory) / "blueprints.sqlite3")
            control = GraphBlueprintControlService(registry)
            source = control.save(blueprint())
            candidate = GraphBlueprint(
                blueprint_id=source.blueprint_id,
                version=2,
                objective_class=source.objective_class,
                execution_profiles=source.execution_profiles,
                parameters=source.parameters,
                tasks=(
                    GraphBlueprintTask(
                        task_id="analysis",
                        objective_template="Assess {{objective}} with explicit release evidence",
                        depends_on=(),
                        required_capabilities=("analysis",),
                        acceptance_templates=("Risk evidence for {{requested_outcome}}",),
                    ),
                    source.tasks[1],
                ),
                final_task_id=source.final_task_id,
                origin=GraphBlueprintOrigin.USER_REVISION,
                parent_ref=source.ref,
            )

            saved, receipt = control.revise(
                source.ref,
                candidate,
                rationale="Clarify the evidence expected from the analysis task.",
            )
            receipts = control.revision_receipts(source.blueprint_id)
            registry.close()

        self.assertEqual(saved.ref, candidate.ref)
        self.assertEqual(receipt.status, BlueprintRevisionStatus.ACCEPTED)
        self.assertEqual(receipt.source_ref, source.ref)
        self.assertEqual(receipt.candidate_ref, candidate.ref)
        self.assertEqual(receipts, (receipt,))

    def test_rejected_user_revision_keeps_a_receipt_without_saving_candidate(self) -> None:
        registry = GraphBlueprintRegistry()
        control = GraphBlueprintControlService(registry)
        source = control.save(blueprint())
        candidate = GraphBlueprint(
            blueprint_id=source.blueprint_id,
            version=3,
            objective_class=source.objective_class,
            execution_profiles=source.execution_profiles,
            parameters=source.parameters,
            tasks=source.tasks,
            final_task_id=source.final_task_id,
            origin=GraphBlueprintOrigin.USER_REVISION,
            parent_ref=source.ref,
        )

        with self.assertRaisesRegex(ValueError, "REVISION_MUST_INCREMENT_BY_ONE"):
            control.revise(source.ref, candidate, rationale="Skip a version.")

        receipts = control.revision_receipts(source.blueprint_id)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].status, BlueprintRevisionStatus.REJECTED)
        self.assertEqual(receipts[0].reason, "REVISION_MUST_INCREMENT_BY_ONE")
        with self.assertRaisesRegex(ValueError, "revision is not available"):
            control.revision(source.blueprint_id, 3)

    def test_revision_diff_describes_structure_without_copying_templates(self) -> None:
        registry = GraphBlueprintRegistry()
        control = GraphBlueprintControlService(registry)
        source = control.save(blueprint())
        candidate = GraphBlueprint(
            blueprint_id=source.blueprint_id,
            version=2,
            objective_class="research",
            execution_profiles=source.execution_profiles,
            parameters=source.parameters,
            tasks=(
                GraphBlueprintTask(
                    task_id="analysis",
                    objective_template="Assess {{objective}} with independent evidence",
                    depends_on=(),
                    required_capabilities=("analysis",),
                    acceptance_templates=("Risk evidence for {{requested_outcome}}",),
                ),
                source.tasks[1],
            ),
            final_task_id=source.final_task_id,
            origin=GraphBlueprintOrigin.USER_REVISION,
            parent_ref=source.ref,
        )
        saved, _receipt = control.revise(
            source.ref,
            candidate,
            rationale="Tighten evidence requirements.",
        )
        diff = control.revision_diff(saved.ref)

        assert diff is not None
        self.assertEqual(diff.source_ref, source.ref)
        self.assertEqual(diff.added_task_ids, ())
        self.assertEqual(diff.removed_task_ids, ())
        self.assertEqual(diff.changed_tasks, (("analysis", ("objective",)),))
        self.assertEqual(diff.changed_envelope_fields, ("objective_class",))
        self.assertNotIn("independent evidence", str(diff))


if __name__ == "__main__":
    unittest.main()
