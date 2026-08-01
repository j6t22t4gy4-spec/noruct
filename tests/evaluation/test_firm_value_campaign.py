from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dynamic_firm import __version__
from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, main
from dynamic_firm.coding import CodingWorkResult
from dynamic_firm.evaluation.firm_value_campaign import (
    CampaignEventKind,
    CampaignState,
    FirmValueCampaignStore,
    campaign_status,
    compare_campaign,
    prepare_firm_value_campaign,
    run_next_campaign_slot,
    source_snapshot_revision,
)
from dynamic_firm.evaluation.firm_value import aggregate_firm_value_records
from dynamic_firm.providers.codex_exec import CodexLoginStatus
from dynamic_firm.runtime.models import (
    CompletionEnvelope,
    ModelResponse,
    StructuredOutputResponse,
    Usage,
)
from dynamic_firm.runtime.ports import ModelProviderError, OperationCancelled


class _UnusedProvider:
    async def complete(self, request, cancellation):
        raise AssertionError("The first SOLO counterfactual must not call the native provider")


class _SoloWorker:
    async def execute(self, request, cancellation):
        cancellation.raise_if_cancelled()
        (request.workspace / "calculator.py").write_text(
            "def safe_divide(numerator: float, denominator: float) -> float | None:\n"
            "    if denominator == 0:\n"
            "        return None\n"
            "    return numerator / denominator\n",
            encoding="utf-8",
        )
        return CodingWorkResult(
            summary="Campaign fixture completed.",
            usage=Usage(model_calls=1, input_tokens=13, output_tokens=8),
        )


class _CampaignProvider:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def complete_structured(self, request, cancellation):
        cancellation.raise_if_cancelled()
        if (self.workspace / "identifier.py").exists():
            tasks = [
                {
                    "task_id": "spec_evidence",
                    "objective": "Read specification evidence.",
                    "depends_on": [],
                    "required_capabilities": ["analysis"],
                    "acceptance_criteria": ["Return bounded evidence."],
                    "risk_level": "LOW",
                },
                {
                    "task_id": "test_evidence",
                    "objective": "Read test evidence.",
                    "depends_on": [],
                    "required_capabilities": ["analysis"],
                    "acceptance_criteria": ["Return bounded evidence."],
                    "risk_level": "LOW",
                },
                {
                    "task_id": "implement_change",
                    "objective": "Implement the identifier contract.",
                    "depends_on": ["spec_evidence", "test_evidence"],
                    "required_capabilities": ["implementation"],
                    "acceptance_criteria": ["The fixture validator passes."],
                    "risk_level": "LOW",
                },
            ]
            mode = "GRAPH"
        else:
            tasks = [
                {
                    "task_id": "implement_change",
                    "objective": "Implement the bounded fixture correction.",
                    "depends_on": [],
                    "required_capabilities": ["implementation"],
                    "acceptance_criteria": ["The fixture validator passes."],
                    "risk_level": "LOW",
                }
            ]
            mode = "SOLO"
        return StructuredOutputResponse(
            value={
                "mode": mode,
                "rationale": "Use the smallest dependency-derived fixture plan.",
                "assumptions": [],
                "tasks": tasks,
                "final_task_id": "implement_change",
            },
            usage=Usage(input_tokens=11, output_tokens=7),
            provider_request_id="campaign-compiler",
        )

    async def complete(self, request, cancellation):
        cancellation.raise_if_cancelled()
        return ModelResponse(
            completion=CompletionEnvelope(
                summary="Bounded dependency evidence prepared.",
                acceptance_evidence=("fixture:evidence",),
            ),
            usage=Usage(input_tokens=5, output_tokens=3),
        )


class _CampaignWorker:
    async def execute(self, request, cancellation):
        cancellation.raise_if_cancelled()
        if (request.workspace / "calculator.py").exists():
            (request.workspace / "calculator.py").write_text(
                "def safe_divide(numerator: float, denominator: float) -> float | None:\n"
                "    if denominator == 0:\n"
                "        return None\n"
                "    return numerator / denominator\n",
                encoding="utf-8",
            )
        elif (request.workspace / "identifier.py").exists():
            (request.workspace / "identifier.py").write_text(
                "import re\n\n"
                "def canonical_identifier(value: str) -> str:\n"
                "    return re.sub(r'[\\s_]+', '-', value.strip().lower())\n",
                encoding="utf-8",
            )
        else:
            if request.validation_feedback:
                content = (
                    "def within_window(value: int, lower: int, upper: int) -> bool:\n"
                    "    if lower > upper:\n"
                    "        raise ValueError('lower must not exceed upper')\n"
                    "    return lower <= value <= upper\n"
                )
            else:
                content = (
                    "def within_window(value: int, lower: int, upper: int) -> bool:\n"
                    "    return lower <= value <= upper\n"
                )
            (request.workspace / "window.py").write_text(content, encoding="utf-8")
        return CodingWorkResult(
            summary="Campaign fixture completed.",
            usage=Usage(model_calls=1, input_tokens=13, output_tokens=8),
        )


def _write_source_root(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_runtime.py").write_text("def test_value(): pass\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='noruct'\n", encoding="utf-8")
    return root


def _write_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"noruct-{__version__}.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: noruct\n"
            f"Version: {__version__}\n",
        )
    return path


async def _prepare(root: Path):
    return await prepare_firm_value_campaign(
        root / "campaign",
        wheel=_write_wheel(root / f"noruct-{__version__}-py3-none-any.whl"),
        source_root=_write_source_root(root / "source"),
        command="fixture-codex",
        model_id="fixture-model",
        login_status_factory=lambda command: CodexLoginStatus(
            executable="/fixture/codex",
            installed=True,
            authenticated=True,
        ),
        capability_probe=lambda command: (
            "/fixture/codex",
            True,
            "supported",
        ),
    )


class FirmValueCampaignTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_freezes_source_wheel_and_rehearses_six_without_quota(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = await _prepare(root)
            persisted = campaign_status(root / "campaign")
            source_revision = source_snapshot_revision(root / "source")

        self.assertTrue(prepared.preflight.ready)
        self.assertEqual(prepared.preflight.offline_runs_checked, 6)
        self.assertEqual(prepared.preflight.external_model_calls, 0)
        self.assertFalse(prepared.preflight.quota_consumed)
        self.assertEqual(prepared.preflight.source_revision, source_revision)
        self.assertEqual(persisted.state, CampaignState.READY)
        self.assertEqual((persisted.next_fixture, persisted.next_strategy), ("solo-edit", "solo"))
        self.assertEqual(persisted.event_count, 1)

    async def test_each_confirmation_advances_exactly_one_sealed_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            campaign = root / "campaign"

            with self.assertRaisesRegex(ValueError, "exactly one slot"):
                await run_next_campaign_slot(campaign, confirm_live_quota=False)
            untouched = campaign_status(campaign)
            result = await run_next_campaign_slot(
                campaign,
                confirm_live_quota=True,
                provider_factory=lambda config: _UnusedProvider(),
                coding_worker_factory=lambda config: _SoloWorker(),
            )
            persisted = json.loads(Path(result.record_path).read_text(encoding="utf-8"))

        self.assertEqual(untouched.event_count, 1)
        self.assertEqual(result.event.kind, CampaignEventKind.RUN_RECORDED)
        self.assertEqual(result.status.completed_runs, 1)
        self.assertEqual((result.status.next_fixture, result.status.next_strategy), ("solo-edit", "dynamic"))
        self.assertTrue(persisted["quota_confirmed"])
        self.assertTrue(persisted["source_revision"].startswith("snapshot-sha256:"))

    async def test_provider_failure_is_sealed_and_blocks_comparison_or_retry(self) -> None:
        async def failed_runner(*args, **kwargs):
            raise ModelProviderError(
                "MODEL_TRANSPORT_ERROR",
                "safe failure",
                retryable=True,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            result = await run_next_campaign_slot(
                root / "campaign",
                confirm_live_quota=True,
                live_runner=failed_runner,
            )
            failure_files = tuple((root / "campaign" / "failures").glob("*.json"))
            failure = json.loads(failure_files[0].read_text(encoding="utf-8"))
            with self.assertRaisesRegex(ValueError, "PARTIAL_FAILED"):
                await run_next_campaign_slot(
                    root / "campaign",
                    confirm_live_quota=True,
                    live_runner=failed_runner,
                )

        self.assertEqual(result.event.kind, CampaignEventKind.RUN_FAILED)
        self.assertEqual(result.status.state, CampaignState.PARTIAL_FAILED)
        self.assertEqual(result.status.failed_runs, 1)
        self.assertEqual(failure["failure_code"], "MODEL_TRANSPORT_ERROR")
        self.assertFalse(failure["partial_result_promoted"])

    async def test_source_drift_blocks_before_a_quota_bearing_run_is_reserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            campaign = root / "campaign"
            (root / "source" / "src" / "runtime.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "source snapshot changed"):
                await run_next_campaign_slot(
                    campaign,
                    confirm_live_quota=True,
                    live_runner=lambda *args, **kwargs: self.fail("runner must not start"),
                )
            status = campaign_status(campaign)

        self.assertEqual(status.state, CampaignState.READY)
        self.assertEqual(status.event_count, 1)

    async def test_hash_chain_tamper_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            campaign = root / "campaign"
            with FirmValueCampaignStore(campaign) as store:
                store.connection.execute(
                    "UPDATE campaign_events SET payload_json = ? WHERE sequence = 1",
                    ('{"ready":false}',),
                )
                store.connection.commit()
            with self.assertRaisesRegex(ValueError, "hash chain"):
                campaign_status(campaign)

    async def test_cancellation_is_a_terminal_interrupted_envelope(self) -> None:
        async def cancelled_runner(*args, **kwargs):
            raise OperationCancelled("fixture cancellation")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            result = await run_next_campaign_slot(
                root / "campaign",
                confirm_live_quota=True,
                live_runner=cancelled_runner,
            )

        self.assertEqual(result.event.kind, CampaignEventKind.RUN_INTERRUPTED)
        self.assertEqual(result.status.state, CampaignState.INTERRUPTED)
        self.assertEqual(result.status.interrupted_runs, 1)
        self.assertEqual(result.status.failed_runs, 0)

    async def test_six_sealed_slots_compare_without_an_aggregator_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            campaign = root / "campaign"
            for _ in range(6):
                result = await run_next_campaign_slot(
                    campaign,
                    confirm_live_quota=True,
                    provider_factory=lambda config: _CampaignProvider(config.workspace),
                    coding_worker_factory=lambda config: _CampaignWorker(),
                )
                self.assertIsNotNone(result.record_path)
            report = compare_campaign(campaign)
            final = campaign_status(campaign)
            report_payload = json.loads((campaign / "report.json").read_text(encoding="utf-8"))

        self.assertEqual(final.state, CampaignState.COMPLETE)
        self.assertEqual(final.completed_runs, 6)
        self.assertEqual(len(report.aggregate_report.pairs), 3)
        self.assertEqual(report.aggregator_provider_calls, 0)
        self.assertFalse(report.aggregator_quota_consumed)
        self.assertFalse(report.campaign_gate_passed)
        self.assertEqual(report.outcome, "DYNAMIC_VALUE_GATE_NOT_MET")
        self.assertEqual(report_payload["benchmark_id"], final.benchmark_id)

    async def test_strict_gate_requires_two_meaningful_attributed_quality_gains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            campaign = root / "campaign"
            for _ in range(6):
                await run_next_campaign_slot(
                    campaign,
                    confirm_live_quota=True,
                    provider_factory=lambda config: _CampaignProvider(config.workspace),
                    coding_worker_factory=lambda config: _CampaignWorker(),
                )
            status = campaign_status(campaign)
            baseline = aggregate_firm_value_records(
                campaign / "manifest.json",
                tuple(Path(path) for path in status.record_paths),
            )
            promoted_pairs = tuple(
                replace(
                    pair,
                    quality_delta=0.3333,
                    value_signal=True,
                    dynamic_maximum_parallelism=2,
                )
                if index < 2
                else pair
                for index, pair in enumerate(baseline.pairs)
            )
            with patch(
                "dynamic_firm.evaluation.firm_value_campaign.aggregate_firm_value_records",
                return_value=replace(baseline, pairs=promoted_pairs),
            ):
                report = compare_campaign(campaign)

        self.assertEqual(report.quality_gain_pair_count, 2)
        self.assertEqual(report.dependency_attributed_value_count, 2)
        self.assertTrue(report.campaign_gate_passed)
        self.assertEqual(report.outcome, "DYNAMIC_VALUE_GATE_PASSED")


class FirmValueCampaignCliTests(unittest.TestCase):
    def test_status_is_read_only_and_run_requires_one_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "campaign"
            with patch(
                "dynamic_firm.evaluation.firm_value_campaign.CodexExecProvider.login_status",
                return_value=CodexLoginStatus(
                    executable="/fixture/codex",
                    installed=True,
                    authenticated=True,
                ),
            ), patch(
                "dynamic_firm.evaluation.firm_value_campaign.probe_codex_structured_output",
                return_value=("/fixture/codex", True, "supported"),
            ):
                prepare_output = io.StringIO()
                prepare_error = io.StringIO()
                prepare_code = main(
                    [
                        "eval",
                        "firm-campaign",
                        "prepare",
                        str(campaign),
                        "--wheel",
                        str(_write_wheel(root / f"noruct-{__version__}-py3-none-any.whl")),
                        "--source-root",
                        str(_write_source_root(root / "source")),
                        "--model",
                        "fixture-model",
                        "--codex-command",
                        "fixture-codex",
                        "--json",
                    ],
                    stdout=prepare_output,
                    stderr=prepare_error,
                )
            status_output = io.StringIO()
            status_error = io.StringIO()
            status_code = main(
                ["eval", "firm-campaign", "status", str(campaign), "--json"],
                stdout=status_output,
                stderr=status_error,
            )
            denied_error = io.StringIO()
            denied_code = main(
                ["eval", "firm-campaign", "run-next", str(campaign)],
                stdout=io.StringIO(),
                stderr=denied_error,
            )

        self.assertEqual(prepare_code, EXIT_OK, prepare_error.getvalue())
        self.assertEqual(status_code, EXIT_OK, status_error.getvalue())
        self.assertEqual(json.loads(status_output.getvalue())["state"], "READY")
        self.assertEqual(denied_code, EXIT_INPUT)
        self.assertIn("--confirm-live-quota", denied_error.getvalue())


if __name__ == "__main__":
    unittest.main()
