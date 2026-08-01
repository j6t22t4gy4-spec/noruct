from __future__ import annotations

import io
import json
import tempfile
import unittest
import uuid
import zipfile
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from dynamic_firm import __version__
from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, main
from dynamic_firm.company.models import content_digest
from dynamic_firm.evaluation.firm_value_campaign import (
    CampaignEventKind,
    CampaignState,
    campaign_status,
    prepare_firm_value_campaign,
)
from dynamic_firm.evaluation.firm_value_campaign_v2 import (
    FirmValueCampaignV2Store,
    campaign_v2_status,
    compare_campaign_v2,
    prepare_firm_value_campaign_v2,
    run_next_campaign_v2_slot,
)
from dynamic_firm.evaluation.firm_value_v2 import (
    FIRM_VALUE_V2_EVALUATOR_PROFILE,
    FIRM_VALUE_V2_LIVE_EVIDENCE_CLASS,
    FIRM_VALUE_V2_LIVE_SCHEMA,
    LiveFirmValueV2Record,
    run_firm_value_v2_case,
)
from dynamic_firm.providers.codex_exec import CodexLoginStatus
from dynamic_firm.runtime.models import to_primitive, utc_now


def _write_source_root(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "runtime.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "tests" / "test_runtime.py").write_text(
        "def test_value(): pass\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname='noruct'\n", encoding="utf-8"
    )
    return root


def _write_wheel(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"noruct-{__version__}.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: noruct\n"
            f"Version: {__version__}\n",
        )
    return path


async def _prepare(root: Path, name: str = "campaign-v2"):
    return await prepare_firm_value_campaign_v2(
        root / name,
        wheel=_write_wheel(root / f"noruct-{__version__}-py3-none-any.whl"),
        source_root=_write_source_root(root / "source"),
        command="fixture-codex",
        model_id="fixture-model",
        login_status_factory=lambda command: CodexLoginStatus(
            executable="/fixture/codex",
            installed=True,
            authenticated=True,
        ),
        capability_probe=lambda command: ("/fixture/codex", True, "supported"),
    )


async def _sealed_live_runner(config, fixture, strategy, **kwargs):
    offline = await run_firm_value_v2_case(fixture, strategy)
    result = replace(
        offline,
        evidence_class=FIRM_VALUE_V2_LIVE_EVIDENCE_CLASS,
        cost=replace(offline.cost, measured_elapsed_ms=1),
    )
    payload = {
        "schema_version": FIRM_VALUE_V2_LIVE_SCHEMA,
        "recorded_at": utc_now().isoformat(),
        "noruct_version": __version__,
        "source_revision": config.source_revision,
        "distribution_sha256": config.distribution_sha256,
        "evaluation_run_id": f"firm-value-v2-live-{uuid.uuid4().hex}",
        "provider_kind": "openai-codex-user-managed",
        "model_id": config.model,
        "planner_source": (
            "live-dynamic-workflow-compiler"
            if str(strategy) == "dynamic"
            else "bounded-counterfactual-plan"
        ),
        "company_revision": config.company_revision,
        "roster_revision": config.roster_revision,
        "playbook_revision": config.playbook_revision,
        "permission_mode": "shadow-workspace-approved",
        "approval_mode": "allow-once",
        "configured_model_call_limit": config.max_total_model_calls,
        "configured_wall_time_ms": config.max_wall_time_ms,
        "quota_confirmed": True,
        "evaluator_risk_confirmed": True,
        "evaluator_profile": FIRM_VALUE_V2_EVALUATOR_PROFILE,
        "elapsed_ms": 1,
        "external_model_calls": result.cost.runtime_model_calls,
        "result": result,
    }
    digest = content_digest(payload)
    return LiveFirmValueV2Record(
        evidence_id=f"firm-value-v2-live-evidence-{digest[:24]}",
        content_hash=digest,
        **payload,
    )


async def _failed_dynamic_control_runner(config, fixture, strategy, **kwargs):
    base = await _sealed_live_runner(config, fixture, strategy, **kwargs)
    if (
        str(fixture) != "solo-edit"
        or str(strategy) != "dynamic"
    ):
        return base
    artifact = replace(
        base.result.artifact,
        passed=False,
        exact_checks_passed=False,
        requested_change_match=False,
        quality_score=0.0,
        passed_check_count=0,
        changed_paths=(),
        checks=tuple(
            replace(check, passed=False, message="failed:control")
            for check in base.result.artifact.checks
        ),
    )
    diagnostics = replace(
        base.result.diagnostics,
        failure_family="VALIDATION",
        terminal_stage="VALIDATION",
        failure_reason="Control validation failed.",
        validation_attempts=(False,),
    )
    result = replace(
        base.result,
        status="FAILED",
        task_success=False,
        artifact=artifact,
        diagnostics=diagnostics,
    )
    payload = {**base.content_payload(), "result": result}
    digest = content_digest(payload)
    return LiveFirmValueV2Record(
        evidence_id=f"firm-value-v2-live-evidence-{digest[:24]}",
        content_hash=digest,
        **payload,
    )


class FirmValueCampaignV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_freezes_exact_4x2_without_quota_and_discloses_risk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = await _prepare(root)
            persisted = campaign_v2_status(root / "campaign-v2")

            self.assertTrue((root / "campaign-v2" / "campaign-v2.db").is_file())
            self.assertFalse((root / "campaign-v2" / "campaign.db").exists())

        self.assertTrue(prepared.preflight.ready)
        self.assertEqual(prepared.preflight.offline_runs_checked, 8)
        self.assertEqual(prepared.preflight.external_model_calls, 0)
        self.assertFalse(prepared.preflight.quota_consumed)
        self.assertFalse(prepared.preflight.evaluator_network_isolated)
        self.assertFalse(prepared.preflight.evaluator_credential_inheritance)
        self.assertTrue(prepared.preflight.evaluator_risk_confirmation_required)
        self.assertEqual(persisted.state, CampaignState.READY)
        self.assertEqual((persisted.next_fixture, persisted.next_strategy), ("solo-edit", "solo"))
        self.assertEqual(persisted.expected_runs, 8)
        self.assertEqual(persisted.event_count, 1)

    async def test_each_slot_requires_two_confirmations_before_ledger_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            campaign = root / "campaign-v2"

            with self.assertRaisesRegex(ValueError, "confirm-live-quota"):
                await run_next_campaign_v2_slot(
                    campaign,
                    confirm_live_quota=False,
                    confirm_evaluator_risk=False,
                )
            self.assertEqual(campaign_v2_status(campaign).event_count, 1)
            with self.assertRaisesRegex(ValueError, "confirm-evaluator-risk"):
                await run_next_campaign_v2_slot(
                    campaign,
                    confirm_live_quota=True,
                    confirm_evaluator_risk=False,
                )
            self.assertEqual(campaign_v2_status(campaign).event_count, 1)

            result = await run_next_campaign_v2_slot(
                campaign,
                confirm_live_quota=True,
                confirm_evaluator_risk=True,
                live_runner=_sealed_live_runner,
            )
            self.assertEqual(result.event.kind, CampaignEventKind.RUN_RECORDED)
            self.assertEqual(result.status.completed_runs, 1)
            self.assertEqual(result.status.event_count, 3)
            self.assertEqual(
                (result.status.next_fixture, result.status.next_strategy),
                ("solo-edit", "dynamic"),
            )
            path = Path(result.record_path)
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sealed record changed"):
                campaign_v2_status(campaign)

    async def test_exact_eight_sealed_records_compare_without_aggregator_quota(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            campaign = root / "campaign-v2"
            for _ in range(8):
                await run_next_campaign_v2_slot(
                    campaign,
                    confirm_live_quota=True,
                    confirm_evaluator_risk=True,
                    live_runner=_sealed_live_runner,
                )
            before = campaign_v2_status(campaign)
            report = compare_campaign_v2(campaign)
            after = campaign_v2_status(campaign)

        self.assertEqual(before.state, CampaignState.COMPLETE)
        self.assertEqual(before.completed_runs, 8)
        self.assertTrue(report.campaign_gate_passed)
        self.assertEqual(report.value_gain_count, 2)
        self.assertEqual(report.aggregator_provider_calls, 0)
        self.assertFalse(report.aggregator_quota_consumed)
        self.assertEqual(after.event_count, before.event_count + 1)

    async def test_failed_control_record_stops_before_the_next_quota_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            campaign = root / "campaign-v2"
            first = await run_next_campaign_v2_slot(
                campaign,
                confirm_live_quota=True,
                confirm_evaluator_risk=True,
                live_runner=_failed_dynamic_control_runner,
            )
            self.assertTrue(first.task_success)
            second = await run_next_campaign_v2_slot(
                campaign,
                confirm_live_quota=True,
                confirm_evaluator_risk=True,
                live_runner=_failed_dynamic_control_runner,
            )
            events_before = second.status.event_count

            self.assertFalse(second.task_success)
            self.assertEqual(second.status.state, CampaignState.PARTIAL_FAILED)
            self.assertFalse(second.status.viable)
            self.assertEqual(
                second.status.stop_reason,
                "CONTROL_GATE_IMPOSSIBLE:solo-edit/dynamic",
            )
            self.assertIsNone(second.status.next_fixture)
            with self.assertRaisesRegex(ValueError, "state=PARTIAL_FAILED"):
                await run_next_campaign_v2_slot(
                    campaign,
                    confirm_live_quota=True,
                    confirm_evaluator_risk=True,
                    live_runner=_sealed_live_runner,
                )
            self.assertEqual(campaign_v2_status(campaign).event_count, events_before)

    async def test_v1_and_v2_campaign_control_planes_refuse_mixing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared_v2 = await _prepare(root, "v2")
            self.assertTrue(prepared_v2.preflight.ready)
            with self.assertRaises((ValueError, FileNotFoundError)):
                campaign_status(root / "v2")

            other = Path(directory) / "other"
            await prepare_firm_value_campaign(
                other / "v1",
                wheel=_write_wheel(other / f"noruct-{__version__}-py3-none-any.whl"),
                source_root=_write_source_root(other / "source"),
                command="fixture-codex",
                model_id="fixture-model",
                login_status_factory=lambda command: CodexLoginStatus(
                    executable="/fixture/codex", installed=True, authenticated=True
                ),
                capability_probe=lambda command: ("/fixture/codex", True, "supported"),
            )
            with self.assertRaisesRegex(ValueError, "refuses a v1"):
                campaign_v2_status(other / "v1")
            with self.assertRaisesRegex(ValueError, "must be empty"):
                await prepare_firm_value_campaign_v2(
                    other / "v1",
                    wheel=other / f"noruct-{__version__}-py3-none-any.whl",
                    source_root=other / "source",
                    command="fixture-codex",
                    model_id="fixture-model",
                )

    async def test_failure_is_terminal_and_source_drift_is_refused_before_reservation(self) -> None:
        async def failing_runner(*args, **kwargs):
            raise RuntimeError("fixture provider failed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            campaign = root / "campaign-v2"
            result = await run_next_campaign_v2_slot(
                campaign,
                confirm_live_quota=True,
                confirm_evaluator_risk=True,
                live_runner=failing_runner,
            )
            self.assertEqual(result.event.kind, CampaignEventKind.RUN_FAILED)
            self.assertEqual(result.status.state, CampaignState.PARTIAL_FAILED)
            self.assertTrue(tuple((campaign / "failures-v2").glob("*.json")))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            campaign = root / "campaign-v2"
            (root / "source" / "src" / "runtime.py").write_text(
                "VALUE = 3\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "source snapshot changed"):
                await run_next_campaign_v2_slot(
                    campaign,
                    confirm_live_quota=True,
                    confirm_evaluator_risk=True,
                    live_runner=_sealed_live_runner,
                )
            self.assertEqual(campaign_v2_status(campaign).event_count, 1)


class FirmValueCampaignV2CliTests(unittest.TestCase):
    def test_run_next_cli_requires_both_confirmations(self) -> None:
        output = io.StringIO()
        error = io.StringIO()

        code = main(
            ["eval", "firm-campaign-v2", "run-next", "/tmp/not-opened"],
            stdout=output,
            stderr=error,
        )

        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("confirm-live-quota", error.getvalue())

    def test_help_exposes_distinct_v2_control_plane(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["eval", "firm-campaign-v2", "run-next", "--help"])

        self.assertEqual(raised.exception.code, EXIT_OK)
        self.assertIn("--confirm-live-quota", output.getvalue())
        self.assertIn("--confirm-evaluator-risk", output.getvalue())


if __name__ == "__main__":
    unittest.main()
