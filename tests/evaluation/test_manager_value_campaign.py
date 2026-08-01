from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.company.models import content_digest
from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.manager_value_campaign import (
    MANAGER_CAMPAIGN_RECORD_SCHEMA,
    ManagerValueLiveRecord,
    ManagerValueCampaignStore,
    _load_record,
    create_manager_value_campaign_report,
    manager_value_campaign_status,
    preflight_manager_value_campaign,
    prepare_manager_value_campaign,
    run_next_manager_value_slot,
    seal_next_manager_value_slot,
)
from dynamic_firm.evaluation.firm_value_campaign import CampaignEventKind, CampaignState
from dynamic_firm.evaluation.manager_value_contract import manager_value_qualification_contract
from dynamic_firm.runtime.models import to_primitive, utc_now


class ManagerValueCampaignTests(unittest.TestCase):
    def _record(self, *, fixture: str, fixture_revision: str, arm: str) -> ManagerValueLiveRecord:
        shape = {"SINGLE_EMPLOYEE": (1, 1, False), "HOMOGENEOUS_GRAPH": (1, 1, False), "HETEROGENEOUS_GRAPH": (2, 2, False), "MANAGER_LED_FIRM": (0, 0, True)}[arm]
        base = ManagerValueLiveRecord(
            MANAGER_CAMPAIGN_RECORD_SCHEMA, "pending", "pending", utc_now().isoformat(), fixture,
            fixture_revision, arm, "snapshot-sha256:" + "a" * 64, "b" * 64, "test-model", 0, 0, 0,
            6, 180_000, True, True, True, True, 1.0, 2, 10, *shape,
            manager_planning_owner_id=("manager-value-executive" if arm == "MANAGER_LED_FIRM" else ""),
            manager_planning_assignment_digest=("c" * 64 if arm == "MANAGER_LED_FIRM" else ""),
            manager_planning_brief_digest=("d" * 64 if arm == "MANAGER_LED_FIRM" else ""),
            compiler_planning_exercised=(arm == "MANAGER_LED_FIRM"),
            execution_replica_count=(2 if arm == "HOMOGENEOUS_GRAPH" else 0),
            replica_group_count=(1 if arm == "HOMOGENEOUS_GRAPH" else 0),
            planning_mode="SOLO_FALLBACK",
            planning_reason="COMPILER_PROVIDER_FAILURE",
            failure_reason_safe="",
            employee_failure_codes=(),
            task_attempt_count=1,
            successful_task_attempt_count=1,
            validation_attempt_count=1,
        )
        digest = content_digest(base.content_payload())
        return ManagerValueLiveRecord(**{**to_primitive(base), "record_id": f"manager-value-live-{digest[:24]}", "content_hash": digest})

    def test_v4_record_keeps_historical_replica_fields_in_its_digest(self) -> None:
        """Legacy v4 evidence is auditable but not current promotion evidence."""

        fixture = manager_value_qualification_contract().fixtures[0]
        current = self._record(
            fixture=fixture.fixture,
            fixture_revision=fixture.fixture_revision,
            arm="HOMOGENEOUS_GRAPH",
        )
        payload = to_primitive(current)
        payload["schema_version"] = "noruct.manager-value-live-record.v4"
        for key in (
            "planning_mode", "planning_reason", "failure_reason_safe",
            "employee_failure_codes", "task_attempt_count",
            "successful_task_attempt_count", "approvals_requested",
            "approvals_granted", "reported_cost_usd", "cost_accounting_mode",
            "validation_attempt_count", "validation_recovery_attempt_count",
            "validation_recovery_success_count",
            "runtime_user_intervention_count", "external_effect_error_count",
            "external_effect_unknown_count", "intervention_accounting_mode",
            "external_effect_accounting_mode",
            "record_id", "content_hash",
        ):
            payload.pop(key, None)
        digest = content_digest(payload)
        legacy = {
            **payload,
            "record_id": f"manager-value-live-{digest[:24]}",
            "content_hash": digest,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy-v4.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = _load_record(path)
        self.assertEqual(loaded.schema_version, "noruct.manager-value-live-record.v4")
        self.assertEqual(loaded.execution_replica_count, 2)

    def test_v7_record_keeps_pre_metrics_content_hash(self) -> None:
        """The new tail metrics do not rewrite already sealed v7 evidence."""

        fixture = manager_value_qualification_contract().fixtures[0]
        current = self._record(
            fixture=fixture.fixture,
            fixture_revision=fixture.fixture_revision,
            arm="SINGLE_EMPLOYEE",
        )
        payload = to_primitive(current)
        payload["schema_version"] = "noruct.manager-value-live-record.v7"
        for key in (
            "validation_attempt_count",
            "validation_recovery_attempt_count",
            "validation_recovery_success_count",
            "runtime_user_intervention_count",
            "external_effect_error_count",
            "external_effect_unknown_count",
            "intervention_accounting_mode",
            "external_effect_accounting_mode",
            "record_id",
            "content_hash",
        ):
            payload.pop(key, None)
        digest = content_digest(payload)
        legacy = {
            **payload,
            "record_id": f"manager-value-live-{digest[:24]}",
            "content_hash": digest,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy-v7.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = _load_record(path)
        self.assertEqual(loaded.schema_version, "noruct.manager-value-live-record.v7")
        self.assertEqual(loaded.validation_attempt_count, 0)

    def test_prepare_and_seal_requires_two_confirmations_and_shape(self) -> None:
        contract = manager_value_qualification_contract()
        fixture, arm = contract.exact_slots[0]
        revision = next(item.fixture_revision for item in contract.fixtures if item.fixture == fixture)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("dynamic_firm.evaluation.manager_value_campaign.source_snapshot_revision", return_value="snapshot-sha256:" + "a" * 64), patch("dynamic_firm.evaluation.manager_value_campaign.wheel_distribution_sha256", return_value="b" * 64):
                status = prepare_manager_value_campaign(root / "campaign", wheel=root / "noruct.whl", source_root=root, model_id="test-model")
            self.assertEqual(status.expected_runs, 16)
            record = self._record(fixture=fixture, fixture_revision=revision, arm=arm)
            evidence = root / "record.json"
            evidence.write_text(json.dumps(to_primitive(record)), encoding="utf-8")
            with patch("dynamic_firm.evaluation.manager_value_campaign.source_snapshot_revision", return_value="snapshot-sha256:" + "a" * 64), patch("dynamic_firm.evaluation.manager_value_campaign.wheel_distribution_sha256", return_value="b" * 64):
                with self.assertRaises(ValueError):
                    seal_next_manager_value_slot(root / "campaign", record_path=evidence, confirm_live_quota=True, confirm_evaluator_risk=False)
                status = seal_next_manager_value_slot(root / "campaign", record_path=evidence, confirm_live_quota=True, confirm_evaluator_risk=True)
            self.assertEqual(status.completed_runs, 1)
            self.assertEqual(status.next_arm, contract.exact_slots[1][1])
            self.assertTrue(manager_value_campaign_status(root / "campaign").ledger_verified)

    def test_v4_manifest_keeps_sealed_fixture_revision_independent_of_later_contract_drift(self) -> None:
        """Historical evidence must not be reinterpreted through today's fixture."""

        contract = manager_value_qualification_contract()
        fixture, arm = contract.exact_slots[0]
        revision = next(item.fixture_revision for item in contract.fixtures if item.fixture == fixture)
        drifted_contract = replace(
            contract,
            fixtures=tuple(
                replace(item, fixture_revision=f"changed-{item.fixture_revision}")
                for item in contract.fixtures
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("dynamic_firm.evaluation.manager_value_campaign.source_snapshot_revision", return_value="snapshot-sha256:" + "a" * 64), patch("dynamic_firm.evaluation.manager_value_campaign.wheel_distribution_sha256", return_value="b" * 64):
                prepare_manager_value_campaign(root / "campaign", wheel=root / "noruct.whl", source_root=root, model_id="test-model")
                evidence = root / "record.json"
                evidence.write_text(json.dumps(to_primitive(self._record(fixture=fixture, fixture_revision=revision, arm=arm))), encoding="utf-8")
                status = seal_next_manager_value_slot(root / "campaign", record_path=evidence, confirm_live_quota=True, confirm_evaluator_risk=True)
            self.assertEqual(status.completed_runs, 1)
            manifest = json.loads((root / "campaign" / "manager-value-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "noruct.manager-value-campaign.v4")
            self.assertIn([fixture, revision], manifest["fixture_revisions"])
            with patch("dynamic_firm.evaluation.manager_value_campaign.manager_value_qualification_contract", return_value=drifted_contract):
                verified = manager_value_campaign_status(root / "campaign")
            self.assertTrue(verified.ledger_verified)
            self.assertEqual(verified.completed_runs, 1)

    def test_started_slot_is_not_reused_after_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("dynamic_firm.evaluation.manager_value_campaign.source_snapshot_revision", return_value="snapshot-sha256:" + "a" * 64), patch("dynamic_firm.evaluation.manager_value_campaign.wheel_distribution_sha256", return_value="b" * 64):
                prepare_manager_value_campaign(root / "campaign", wheel=root / "noruct.whl", source_root=root, model_id="test-model")
            contract = manager_value_qualification_contract()
            fixture, arm = contract.exact_slots[0]
            with ManagerValueCampaignStore(root / "campaign") as store:
                store.append(CampaignEventKind.RUN_STARTED, fixture=fixture, strategy=arm, payload={"quota_confirmed": True})
            status = manager_value_campaign_status(root / "campaign")
            self.assertEqual(status.state, CampaignState.INTERRUPTED)
            self.assertEqual(status.interrupted_runs, 1)
            self.assertEqual(status.external_model_calls_forfeited, 6)
            self.assertEqual(status.external_model_calls_accounted, 6)
            self.assertIsNone(status.next_fixture)

    def test_preflight_is_read_only_and_exposes_frozen_execution_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("dynamic_firm.evaluation.manager_value_campaign.source_snapshot_revision", return_value="snapshot-sha256:" + "a" * 64), patch("dynamic_firm.evaluation.manager_value_campaign.wheel_distribution_sha256", return_value="b" * 64):
                prepare_manager_value_campaign(
                    root / "campaign",
                    wheel=root / "noruct.whl",
                    source_root=root,
                    model_id="test-model",
                    codex_command="test-codex",
                )
                with patch("dynamic_firm.evaluation.manager_value_campaign.shutil.which", return_value="/usr/bin/test-codex"):
                    preflight = preflight_manager_value_campaign(root / "campaign")
            self.assertTrue(preflight.ready)
            self.assertEqual(preflight.external_model_calls, 0)
            self.assertFalse(preflight.quota_consumed)
            self.assertEqual(preflight.next_fixture, manager_value_qualification_contract().exact_slots[0][0])
            self.assertTrue(all(check.passed for check in preflight.checks))

    def test_cli_preflight_exposes_readiness_without_running_a_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("dynamic_firm.evaluation.manager_value_campaign.source_snapshot_revision", return_value="snapshot-sha256:" + "a" * 64), patch("dynamic_firm.evaluation.manager_value_campaign.wheel_distribution_sha256", return_value="b" * 64):
                prepare_manager_value_campaign(
                    root / "campaign",
                    wheel=root / "noruct.whl",
                    source_root=root,
                    model_id="test-model",
                    codex_command="test-codex",
                )
                output, error = io.StringIO(), io.StringIO()
                with patch("dynamic_firm.evaluation.manager_value_campaign.shutil.which", return_value="/usr/bin/test-codex"):
                    code = main(
                        ["eval", "manager-campaign", "preflight", str(root / "campaign"), "--json"],
                        stdout=output,
                        stderr=error,
                    )
            self.assertEqual(code, EXIT_OK, error.getvalue())
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["ready"])
            self.assertEqual(payload["external_model_calls"], 0)

    def test_all_sixteen_sealed_records_produce_read_only_comparison_report(self) -> None:
        contract = manager_value_qualification_contract()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("dynamic_firm.evaluation.manager_value_campaign.source_snapshot_revision", return_value="snapshot-sha256:" + "a" * 64), patch("dynamic_firm.evaluation.manager_value_campaign.wheel_distribution_sha256", return_value="b" * 64):
                prepare_manager_value_campaign(root / "campaign", wheel=root / "noruct.whl", source_root=root, model_id="test-model")
                for index, (fixture, arm) in enumerate(contract.exact_slots):
                    revision = next(item.fixture_revision for item in contract.fixtures if item.fixture == fixture)
                    evidence = root / f"record-{index}.json"
                    evidence.write_text(
                        json.dumps(to_primitive(self._record(fixture=fixture, fixture_revision=revision, arm=arm))),
                        encoding="utf-8",
                    )
                    status = seal_next_manager_value_slot(
                        root / "campaign",
                        record_path=evidence,
                        confirm_live_quota=True,
                        confirm_evaluator_risk=True,
                    )
            self.assertEqual(status.state, CampaignState.COMPLETE)
            self.assertEqual(status.completed_runs, 16)
            self.assertEqual(status.external_model_calls_recorded, 32)
            report = create_manager_value_campaign_report(root / "campaign")
            self.assertTrue(report.qualified)
            self.assertFalse(report.outcome_claimed)
            self.assertEqual(len(report.outcomes), 4)
            self.assertEqual(report.manager_incremental_quality_vs_heterogeneous, 0.0)
            self.assertTrue(
                all(
                    outcome.mean_approvals_requested
                    >= outcome.mean_approvals_granted
                    for outcome in report.outcomes
                )
            )
            self.assertTrue(
                all(
                    outcome.cost_accounting_mode == "MODEL_CALL_PROXY"
                    and outcome.mean_reported_cost_usd is None
                    for outcome in report.outcomes
                )
            )
            self.assertTrue(
                all(
                    outcome.mean_validation_recovery_attempts == 0
                    and outcome.validation_recovery_success_rate is None
                    and outcome.mean_runtime_user_interventions == 0
                    and outcome.external_effect_error_rate == 0
                    and outcome.external_effect_unknown_rate == 0
                    for outcome in report.outcomes
                )
            )

    def test_manager_slot_rejects_record_without_compiler_planning_provenance(self) -> None:
        contract = manager_value_qualification_contract()
        fixture, arm = next(
            slot for slot in contract.exact_slots
            if slot[1] == "MANAGER_LED_FIRM"
        )
        revision = next(item.fixture_revision for item in contract.fixtures if item.fixture == fixture)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("dynamic_firm.evaluation.manager_value_campaign.source_snapshot_revision", return_value="snapshot-sha256:" + "a" * 64), patch("dynamic_firm.evaluation.manager_value_campaign.wheel_distribution_sha256", return_value="b" * 64):
                prepare_manager_value_campaign(root / "campaign", wheel=root / "noruct.whl", source_root=root, model_id="test-model")
                for index, (next_fixture, next_arm) in enumerate(contract.exact_slots):
                    if (next_fixture, next_arm) == (fixture, arm):
                        break
                    next_revision = next(item.fixture_revision for item in contract.fixtures if item.fixture == next_fixture)
                    evidence = root / f"record-{index}.json"
                    evidence.write_text(json.dumps(to_primitive(self._record(fixture=next_fixture, fixture_revision=next_revision, arm=next_arm))), encoding="utf-8")
                    seal_next_manager_value_slot(root / "campaign", record_path=evidence, confirm_live_quota=True, confirm_evaluator_risk=True)
                missing = replace(
                    self._record(fixture=fixture, fixture_revision=revision, arm=arm),
                    record_id="pending",
                    content_hash="pending",
                    manager_planning_owner_id="",
                    manager_planning_assignment_digest="",
                    manager_planning_brief_digest="",
                    compiler_planning_exercised=False,
                )
                digest = content_digest(missing.content_payload())
                missing = replace(
                    missing,
                    record_id=f"manager-value-live-{digest[:24]}",
                    content_hash=digest,
                )
                evidence = root / "missing-manager-planning.json"
                evidence.write_text(json.dumps(to_primitive(missing)), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Manager planning provenance"):
                    seal_next_manager_value_slot(root / "campaign", record_path=evidence, confirm_live_quota=True, confirm_evaluator_risk=True)

    def test_manager_slot_rejects_frozen_plan_replay_even_with_valid_digests(self) -> None:
        contract = manager_value_qualification_contract()
        fixture, arm = next(
            slot for slot in contract.exact_slots
            if slot[1] == "MANAGER_LED_FIRM"
        )
        revision = next(
            item.fixture_revision for item in contract.fixtures if item.fixture == fixture
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("dynamic_firm.evaluation.manager_value_campaign.source_snapshot_revision", return_value="snapshot-sha256:" + "a" * 64), patch("dynamic_firm.evaluation.manager_value_campaign.wheel_distribution_sha256", return_value="b" * 64):
                prepare_manager_value_campaign(root / "campaign", wheel=root / "noruct.whl", source_root=root, model_id="test-model")
                for index, (next_fixture, next_arm) in enumerate(contract.exact_slots):
                    if (next_fixture, next_arm) == (fixture, arm):
                        break
                    next_revision = next(
                        item.fixture_revision
                        for item in contract.fixtures
                        if item.fixture == next_fixture
                    )
                    evidence = root / f"record-{index}.json"
                    evidence.write_text(
                        json.dumps(to_primitive(self._record(fixture=next_fixture, fixture_revision=next_revision, arm=next_arm))),
                        encoding="utf-8",
                    )
                    seal_next_manager_value_slot(root / "campaign", record_path=evidence, confirm_live_quota=True, confirm_evaluator_risk=True)
                replay = replace(
                    self._record(fixture=fixture, fixture_revision=revision, arm=arm),
                    record_id="pending",
                    content_hash="pending",
                    compiler_planning_exercised=False,
                )
                digest = content_digest(replay.content_payload())
                replay = replace(
                    replay,
                    record_id=f"manager-value-live-{digest[:24]}",
                    content_hash=digest,
                )
                evidence = root / "frozen-manager-replay.json"
                evidence.write_text(json.dumps(to_primitive(replay)), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "exercised Manager planning provenance"):
                    seal_next_manager_value_slot(root / "campaign", record_path=evidence, confirm_live_quota=True, confirm_evaluator_risk=True)

    def test_runtime_failure_is_terminal_evidence_and_cannot_reuse_the_slot(self) -> None:
        async def failing_runner(*_args, **_kwargs):
            raise RuntimeError("synthetic provider failure")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("dynamic_firm.evaluation.manager_value_campaign.source_snapshot_revision", return_value="snapshot-sha256:" + "a" * 64), patch("dynamic_firm.evaluation.manager_value_campaign.wheel_distribution_sha256", return_value="b" * 64):
                prepare_manager_value_campaign(root / "campaign", wheel=root / "noruct.whl", source_root=root, model_id="test-model")
                status = asyncio.run(
                    run_next_manager_value_slot(
                        root / "campaign",
                        confirm_live_quota=True,
                        confirm_evaluator_risk=True,
                        live_runner=failing_runner,
                    )
                )
            self.assertEqual(status.state, CampaignState.PARTIAL_FAILED)
            self.assertEqual(status.failed_runs, 1)
            self.assertEqual(status.external_model_calls_recorded, 0)
            self.assertEqual(status.external_model_calls_forfeited, 6)
            self.assertEqual(status.external_model_calls_accounted, 6)
            self.assertIsNone(status.next_fixture)
            self.assertTrue((root / "campaign" / "failures").is_dir())
            failure = next((root / "campaign" / "failures").glob("*.json"))
            receipt = json.loads(failure.read_text(encoding="utf-8"))
            self.assertEqual(receipt["failure_stage"], "LIVE_RUNNER")
            self.assertEqual(receipt["external_model_calls_accounting"], "UNKNOWN_FORFEITED")
            self.assertEqual(receipt["reserved_external_model_calls"], 6)
