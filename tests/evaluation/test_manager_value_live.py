from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.coding import CodingWorkRequest, CodingWorkResult
from dynamic_firm.evaluation.firm_value_campaign import CampaignState
from dynamic_firm.evaluation.firm_value_v2 import FirmValueV2FixtureKind
from dynamic_firm.evaluation.manager_value_campaign import (
    manager_value_campaign_status,
    prepare_manager_value_campaign,
    run_next_manager_value_slot,
)
from dynamic_firm.evaluation.manager_value_contract import ManagerValueArm
from dynamic_firm.evaluation.manager_value_live import (
    _ReadOnlyEvidenceWorker,
    ManagerValueLiveConfig,
    _arm_plan,
    _selected_capability_profile_count,
    run_live_manager_value_evaluation,
    run_manager_value_offline_case,
    run_manager_value_offline_rehearsal,
)
from dynamic_firm.evaluation.firm_value_v2 import _V2Provider, _V2Worker
from dynamic_firm.kernel.models import EmployeeRecord
from dynamic_firm.runtime.ports import CancellationToken


class ManagerValueLiveTests(unittest.TestCase):
    def test_read_only_evidence_wrapper_routes_by_capability_not_task_id(self) -> None:
        class Worker:
            def __init__(self) -> None:
                self.calls = 0

            async def execute(self, request, cancellation):
                self.calls += 1
                return CodingWorkResult(summary=f"implemented:{request.task_id}")

        async def exercise():
            worker = Worker()
            wrapper = _ReadOnlyEvidenceWorker(worker)
            evidence = await wrapper.execute(
                CodingWorkRequest(
                    task_id="analysis_probe",
                    objective="Read evidence",
                    acceptance_criteria=(),
                    dependency_context=(),
                    workspace=Path.cwd(),
                    model_profile="test",
                    max_wall_time_ms=1_000,
                    required_capabilities=("analysis",),
                ),
                CancellationToken(),
            )
            implementation = await wrapper.execute(
                CodingWorkRequest(
                    task_id="implement_safe_divide_fix",
                    objective="Change calculator.py",
                    acceptance_criteria=(),
                    dependency_context=(),
                    workspace=Path.cwd(),
                    model_profile="test",
                    max_wall_time_ms=1_000,
                    required_capabilities=("implementation",),
                ),
                CancellationToken(),
            )
            return worker.calls, evidence.summary, implementation.summary

        calls, evidence_summary, implementation_summary = asyncio.run(exercise())

        self.assertEqual(calls, 1)
        self.assertEqual(evidence_summary, "Read-only graph evidence collected.")
        self.assertEqual(implementation_summary, "implemented:implement_safe_divide_fix")

    def test_direct_manager_selection_uses_executed_employee_not_empty_graph_template(self) -> None:
        roster = (
            EmployeeRecord("analyst", "Analyst", ("analysis",)),
            EmployeeRecord("writer", "Engineer", ("implementation",)),
        )
        self.assertEqual(
            _selected_capability_profile_count(
                (), roster, ({"employee_id": "writer", "status": "SUCCEEDED"},)
            ),
            1,
        )

    def test_every_arm_uses_a_distinct_kernel_shape(self) -> None:
        async def exercise():
            return {
                arm: await run_manager_value_offline_case(
                    FirmValueV2FixtureKind.SOLO_EDIT,
                    arm,
                )
                for arm in ManagerValueArm
            }

        outcomes = asyncio.run(exercise())
        self.assertTrue(all(item.record.task_success for item in outcomes.values()))
        self.assertEqual(outcomes[ManagerValueArm.SINGLE_EMPLOYEE].record.employee_count, 1)
        self.assertEqual(outcomes[ManagerValueArm.HOMOGENEOUS_GRAPH].record.capability_profile_count, 1)
        self.assertGreaterEqual(
            outcomes[ManagerValueArm.HETEROGENEOUS_GRAPH].record.capability_profile_count,
            2,
        )
        manager = outcomes[ManagerValueArm.MANAGER_LED_FIRM]
        self.assertTrue(manager.record.manager_bound)
        self.assertTrue(manager.manager_assignment_bound)
        self.assertEqual(manager.manager_planning_owner_id, "manager-value-executive")
        self.assertEqual(len(manager.manager_planning_brief_digest), 64)
        self.assertGreater(manager.manager_supervision_count, 0)
        self.assertTrue(manager.record.planning_mode)
        self.assertTrue(manager.record.planning_reason)
        self.assertGreaterEqual(
            manager.record.task_attempt_count,
            manager.record.successful_task_attempt_count,
        )
        self.assertGreaterEqual(
            manager.record.approvals_requested,
            manager.record.approvals_granted,
        )
        self.assertGreaterEqual(manager.record.validation_attempt_count, 1)
        self.assertGreaterEqual(
            manager.record.validation_recovery_attempt_count,
            manager.record.validation_recovery_success_count,
        )
        self.assertEqual(manager.record.runtime_user_intervention_count, 0)
        self.assertEqual(manager.record.external_effect_error_count, 0)
        self.assertEqual(manager.record.external_effect_unknown_count, 0)
        self.assertNotEqual(
            outcomes[ManagerValueArm.SINGLE_EMPLOYEE].plan_task_ids,
            outcomes[ManagerValueArm.HOMOGENEOUS_GRAPH].plan_task_ids,
        )

    def test_run_next_reserves_then_seals_a_runtime_record(self) -> None:
        async def fake_runner(config, fixture, arm, **_):
            outcome = await run_manager_value_offline_case(fixture, arm)
            # The provider-free rehearsal has the same frozen envelope values
            # as this test campaign except source/wheel/model. Reconstructing
            # the record through the live runner is separately tested above;
            # this callback isolates campaign lifecycle behaviour.
            from dataclasses import replace
            from dynamic_firm.company.models import content_digest
            from dynamic_firm.evaluation.manager_value_campaign import ManagerValueLiveRecord
            from dynamic_firm.runtime.models import to_primitive

            base = replace(
                outcome.record,
                record_id="pending",
                content_hash="pending",
                source_revision=config.source_revision,
                distribution_sha256=config.distribution_sha256,
                model_id=config.model,
                company_revision=config.company_revision,
                roster_revision=config.roster_revision,
                playbook_revision=config.playbook_revision,
                configured_model_call_limit=config.max_total_model_calls,
                configured_wall_time_ms=config.max_wall_time_ms,
            )
            digest = content_digest(base.content_payload())
            return ManagerValueLiveRecord(
                **{
                    **to_primitive(base),
                    "record_id": f"manager-value-live-{digest[:24]}",
                    "content_hash": digest,
                }
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            patches = (
                patch(
                    "dynamic_firm.evaluation.manager_value_campaign.source_snapshot_revision",
                    return_value="snapshot-sha256:" + "a" * 64,
                ),
                patch(
                    "dynamic_firm.evaluation.manager_value_campaign.wheel_distribution_sha256",
                    return_value="b" * 64,
                ),
            )
            with patches[0], patches[1]:
                prepare_manager_value_campaign(
                    root / "campaign",
                    wheel=root / "noruct.whl",
                    source_root=root,
                    model_id="test-model",
                )
                status = asyncio.run(
                    run_next_manager_value_slot(
                        root / "campaign",
                        confirm_live_quota=True,
                        confirm_evaluator_risk=True,
                        live_runner=fake_runner,
                    )
                )
            self.assertEqual(status.state, CampaignState.READY)
            self.assertEqual(status.completed_runs, 1)
            self.assertTrue(manager_value_campaign_status(root / "campaign").ledger_verified)

    def test_offline_rehearsal_executes_the_complete_contract_without_quota(self) -> None:
        rehearsal = asyncio.run(run_manager_value_offline_rehearsal())

        self.assertTrue(rehearsal.passed)
        self.assertEqual(len(rehearsal.outcomes), 16)
        self.assertEqual(rehearsal.external_model_calls, 0)
        self.assertFalse(rehearsal.quota_consumed)
        recovery = tuple(
            outcome.record
            for outcome in rehearsal.outcomes
            if outcome.record.fixture
            == FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY.value
        )
        self.assertEqual(len(recovery), 4)
        self.assertTrue(
            all(
                record.validation_attempt_count == 2
                and record.validation_recovery_attempt_count == 1
                and record.validation_recovery_success_count == 1
                for record in recovery
            )
        )
        self.assertTrue(
            all(
                record.runtime_user_intervention_count == 0
                and record.external_effect_error_count == 0
                and record.external_effect_unknown_count == 0
                for record in (outcome.record for outcome in rehearsal.outcomes)
            )
        )

    def test_live_executor_uses_frozen_envelope_without_real_provider_in_test(self) -> None:
        config = ManagerValueLiveConfig(
            command="offline-test",
            model="test-model",
            source_revision="snapshot-sha256:" + "c" * 64,
            distribution_sha256="d" * 64,
            quota_confirmed=True,
            evaluator_risk_confirmed=True,
            roster_revision=1,
        )
        record = asyncio.run(
            run_live_manager_value_evaluation(
                config,
                FirmValueV2FixtureKind.SOLO_EDIT,
                ManagerValueArm.MANAGER_LED_FIRM,
                provider_factory=lambda _: _V2Provider(
                    _arm_plan(
                        FirmValueV2FixtureKind.SOLO_EDIT,
                        ManagerValueArm.MANAGER_LED_FIRM,
                    ),
                    count_compiler=True,
                ),
                coding_worker_factory=lambda _: _V2Worker(
                    FirmValueV2FixtureKind.SOLO_EDIT,
                    strategy="dynamic",
                ),
            )
        )
        self.assertEqual(record.model_id, "test-model")
        self.assertTrue(record.manager_bound)
        self.assertEqual(record.manager_planning_owner_id, "manager-value-executive")
        self.assertEqual(len(record.manager_planning_assignment_digest), 64)
        self.assertTrue(record.compiler_planning_exercised)
        self.assertLessEqual(record.external_model_calls, config.max_total_model_calls)

    def test_live_baseline_keeps_its_frozen_counterfactual_graph(self) -> None:
        config = ManagerValueLiveConfig(
            command="offline-test",
            model="test-model",
            source_revision="snapshot-sha256:" + "c" * 64,
            distribution_sha256="d" * 64,
            quota_confirmed=True,
            evaluator_risk_confirmed=True,
            roster_revision=1,
        )
        record = asyncio.run(
            run_live_manager_value_evaluation(
                config,
                FirmValueV2FixtureKind.SOLO_EDIT,
                ManagerValueArm.HOMOGENEOUS_GRAPH,
                # This provider would be allowed to return any plan if the
                # live baseline did not install its frozen counterfactual.
                provider_factory=lambda _: _V2Provider({}, count_compiler=True),
                coding_worker_factory=lambda _: _V2Worker(
                    FirmValueV2FixtureKind.SOLO_EDIT, strategy="dynamic"
                ),
            )
        )
        self.assertFalse(record.compiler_planning_exercised)
        self.assertFalse(record.manager_bound)
        self.assertGreaterEqual(record.execution_replica_count, 2)
        self.assertGreaterEqual(record.replica_group_count, 1)
        self.assertEqual(record.capability_profile_count, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
