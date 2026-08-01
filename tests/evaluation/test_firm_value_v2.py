from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.coding import CodingWorkResult
from dynamic_firm.evaluation import firm_value_v2 as firm_value_v2_module
from dynamic_firm.evaluation.firm_value_v2 import (
    FIRM_VALUE_V2_LIVE_EVIDENCE_CLASS,
    FIRM_VALUE_V2_LEGACY_RUN_SCHEMA,
    FIRM_VALUE_V2_RUN_SCHEMA,
    FirmValueV2FixtureKind,
    FixturePurpose,
    LiveFirmValueV2Config,
    _V2Provider,
    _V2Worker,
    _plan,
    artifact_score_candidate,
    compare_firm_value_v2_records,
    firm_value_v2_fixture_contract,
    firm_value_v2_to_json,
    load_firm_value_v2_run_record,
    load_live_firm_value_v2_record,
    materialize_firm_value_v2_fixture,
    run_firm_value_v2_matrix,
    run_firm_value_v2_self_test,
    run_live_firm_value_v2_evaluation,
)
from dynamic_firm.evaluation.closed_loop import CodingStrategyKind
from dynamic_firm.runtime.models import Usage


class NoChangeOutputBudgetWorker:
    async def execute(self, request, cancellation):
        cancellation.raise_if_cancelled()
        return CodingWorkResult(
            summary="No validated change was produced.",
            usage=Usage(input_tokens=10, output_tokens=20_000),
            provider_request_id="no-change-output-budget",
        )


class HighInputSuccessfulWorker:
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
            summary="Prepared the validated high-input live v2 candidate.",
            usage=Usage(input_tokens=107_122, output_tokens=1_622),
            provider_request_id="high-input-live-v2",
        )


class SemanticHintRecoveryWorker:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
        feedback = request.validation_feedback
        if feedback:
            if (
                "expect:raise-ValueError-when-lower-greater-than-upper"
                not in feedback[0].detail
            ):
                raise AssertionError("Recovery omitted the evaluator-owned semantic hint")
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
            summary="Prepared the semantic-hint recovery candidate.",
            usage=Usage(input_tokens=17, output_tokens=9),
            provider_request_id=f"semantic-hint-recovery-{len(self.requests)}",
        )


class FirmValueV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_integrated_matrix_separates_controls_and_value_fixtures(self) -> None:
        records = await run_firm_value_v2_matrix()
        report = compare_firm_value_v2_records(records)

        self.assertEqual(len(records), 8)
        self.assertTrue(report.ready_for_live_preflight)
        self.assertTrue(report.safety_gate_passed)
        self.assertTrue(report.control_gate_passed)
        self.assertTrue(report.organization_gate_passed)
        self.assertEqual(report.value_fixture_count, 2)
        self.assertEqual(report.value_gain_count, 2)
        controls = [pair for pair in report.pairs if pair.purpose == FixturePurpose.CONTROL]
        values = [
            pair for pair in report.pairs if pair.purpose == FixturePurpose.VALUE_IDENTIFIABLE
        ]
        self.assertTrue(all(not pair.included_in_gain_denominator for pair in controls))
        self.assertTrue(all(pair.included_in_gain_denominator for pair in values))
        self.assertTrue(all(pair.value_signal for pair in values))

    async def test_artifact_scorer_cannot_observe_topology(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = materialize_firm_value_v2_fixture(
                FirmValueV2FixtureKind.EVIDENCE_SYNTHESIS,
                Path(directory) / "workspace",
            )
            (workspace / "delivery.py").write_text(
                "def route_delivery(channel: str, priority: int, verified: bool) -> str:\n"
                "    if channel not in {'direct', 'bulk'}:\n"
                "        raise ValueError\n"
                "    if type(priority) is not int or not 0 <= priority <= 10:\n"
                "        raise ValueError\n"
                "    if not verified:\n"
                "        return 'hold'\n"
                "    if priority >= 8:\n"
                "        return 'expedite'\n"
                "    return 'batch' if channel == 'bulk' else 'standard'\n",
                encoding="utf-8",
            )

            score = artifact_score_candidate(
                FirmValueV2FixtureKind.EVIDENCE_SYNTHESIS,
                workspace,
            )

        self.assertTrue(score.passed)
        self.assertEqual(score.quality_score, 1.0)
        self.assertEqual(tuple(artifact_score_candidate.__annotations__), ("fixture", "workspace", "return"))

    async def test_v2_run_round_trips_and_refuses_v1_or_mixed_schema(self) -> None:
        records = await run_firm_value_v2_matrix()
        payload = json.loads(firm_value_v2_to_json(records[0]))

        loaded = load_firm_value_v2_run_record(payload)

        self.assertEqual(loaded.schema_version, FIRM_VALUE_V2_RUN_SCHEMA)
        self.assertEqual(loaded, records[0])
        legacy = dict(payload)
        legacy["schema_version"] = FIRM_VALUE_V2_LEGACY_RUN_SCHEMA
        legacy.pop("diagnostics")
        legacy["cost"] = {
            key: legacy["cost"][key]
            for key in ("runtime_model_calls", "total_tokens", "measured_elapsed_ms")
        }
        loaded_legacy = load_firm_value_v2_run_record(legacy)
        self.assertEqual(loaded_legacy.schema_version, FIRM_VALUE_V2_LEGACY_RUN_SCHEMA)
        self.assertEqual(
            loaded_legacy.diagnostics.terminal_stage,
            "LEGACY_DIAGNOSTICS_UNAVAILABLE",
        )
        oversized_diagnostic = json.loads(firm_value_v2_to_json(records[0]))
        oversized_diagnostic["diagnostics"]["failure_reason"] = "x" * 513
        with self.assertRaisesRegex(ValueError, "bounded text contract"):
            load_firm_value_v2_run_record(oversized_diagnostic)
        payload["schema_version"] = "noruct.firm-value-run.v1"
        with self.assertRaisesRegex(ValueError, "refuses non-v2"):
            load_firm_value_v2_run_record(payload)
        with self.assertRaisesRegex(ValueError, "refuses non-v2"):
            compare_firm_value_v2_records(
                (replace(records[0], schema_version="noruct.firm-value-run.v1"),)
            )
        with self.assertRaisesRegex(ValueError, "mixed evidence classes"):
            compare_firm_value_v2_records(
                (
                    replace(
                        records[0],
                        evidence_class=FIRM_VALUE_V2_LIVE_EVIDENCE_CLASS,
                    ),
                    *records[1:],
                )
            )

    async def test_fixture_revision_ignores_cache_and_undeclared_runtime_files(self) -> None:
        fixture = FirmValueV2FixtureKind.SOLO_EDIT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / fixture.value
            shutil.copytree(firm_value_v2_module._fixture_root(fixture), root)
            with patch.object(firm_value_v2_module, "_fixture_root", return_value=root):
                before = firm_value_v2_fixture_contract(fixture).fixture_revision
                (root / "__pycache__").mkdir(exist_ok=True)
                (root / "__pycache__" / "calculator.cpython-311.pyc").write_bytes(
                    b"runtime-cache"
                )
                (root / "runtime-output.log").write_text("noise\n", encoding="utf-8")
                after_runtime_noise = firm_value_v2_fixture_contract(
                    fixture
                ).fixture_revision
                (root / "TASK.md").write_text("changed contract\n", encoding="utf-8")
                after_declared_change = firm_value_v2_fixture_contract(
                    fixture
                ).fixture_revision

        self.assertEqual(before, after_runtime_noise)
        self.assertNotEqual(before, after_declared_change)

    async def test_recovery_control_discloses_its_allowed_change_scope(self) -> None:
        fixture = FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY
        plan = _plan(fixture, CodingStrategyKind.SOLO)
        objective = plan["tasks"][0]["objective"]
        task = (
            firm_value_v2_module._fixture_root(fixture) / "TASK.md"
        ).read_text(encoding="utf-8")

        self.assertIn("change only window.py", objective)
        self.assertIn("change only `window.py`", task)

    async def test_live_failure_preserves_exact_recovery_budget_attribution(self) -> None:
        plan = _plan(FirmValueV2FixtureKind.SOLO_EDIT, CodingStrategyKind.SOLO)
        record = await run_live_firm_value_v2_evaluation(
            LiveFirmValueV2Config(
                command="fixture-codex",
                model="fixture-model",
                source_revision="snapshot-sha256:" + "a" * 64,
                distribution_sha256="b" * 64,
                quota_confirmed=True,
                evaluator_risk_confirmed=True,
            ),
            FirmValueV2FixtureKind.SOLO_EDIT,
            CodingStrategyKind.SOLO,
            provider_factory=lambda provider_config: _V2Provider(
                plan,
                count_compiler=False,
            ),
            coding_worker_factory=lambda provider_config: NoChangeOutputBudgetWorker(),
        )

        self.assertFalse(record.result.task_success)
        self.assertEqual(record.result.diagnostics.failure_family, "BUDGET")
        self.assertEqual(
            record.result.diagnostics.terminal_stage,
            "RECOVERY_ADMISSION",
        )
        self.assertEqual(
            record.result.diagnostics.budget_limit_reasons,
            ("max_output_tokens",),
        )
        self.assertEqual(record.result.diagnostics.worker_attempt_count, 1)
        self.assertEqual(record.result.diagnostics.validation_attempts, (False,))
        self.assertIn(
            "noruct-firm-value-v2-validation=failed:zero-denominator,negative-denominator",
            record.result.diagnostics.failure_reason,
        )
        self.assertIn("changes=none", record.result.diagnostics.failure_reason)
        self.assertEqual(record.result.cost.input_tokens, 10)
        self.assertEqual(record.result.cost.output_tokens, 20_000)

    async def test_live_v2_uses_the_live_input_budget_before_apply(self) -> None:
        plan = _plan(FirmValueV2FixtureKind.SOLO_EDIT, CodingStrategyKind.SOLO)
        record = await run_live_firm_value_v2_evaluation(
            LiveFirmValueV2Config(
                command="fixture-codex",
                model="fixture-model",
                source_revision="snapshot-sha256:" + "a" * 64,
                distribution_sha256="b" * 64,
                quota_confirmed=True,
                evaluator_risk_confirmed=True,
            ),
            FirmValueV2FixtureKind.SOLO_EDIT,
            CodingStrategyKind.SOLO,
            provider_factory=lambda provider_config: _V2Provider(
                plan,
                count_compiler=False,
            ),
            coding_worker_factory=lambda provider_config: HighInputSuccessfulWorker(),
        )

        self.assertTrue(record.result.task_success)
        self.assertEqual(record.result.status, "SUCCEEDED")
        self.assertEqual(record.result.artifact.changed_paths, ("calculator.py",))
        self.assertEqual(record.result.diagnostics.failure_family, "NONE")
        self.assertEqual(record.result.diagnostics.budget_limit_reasons, ())
        self.assertEqual(record.result.cost.input_tokens, 107_122)

    async def test_live_v2_recovery_receives_the_evaluator_owned_semantic_hint(self) -> None:
        fixture = FirmValueV2FixtureKind.TEST_GUIDED_RECOVERY
        worker = SemanticHintRecoveryWorker()
        record = await run_live_firm_value_v2_evaluation(
            LiveFirmValueV2Config(
                command="fixture-codex",
                model="fixture-model",
                source_revision="snapshot-sha256:" + "a" * 64,
                distribution_sha256="b" * 64,
                quota_confirmed=True,
                evaluator_risk_confirmed=True,
            ),
            fixture,
            CodingStrategyKind.SOLO,
            provider_factory=lambda provider_config: _V2Provider(
                _plan(fixture, CodingStrategyKind.SOLO),
                count_compiler=False,
            ),
            coding_worker_factory=lambda provider_config: worker,
        )

        self.assertTrue(record.result.task_success)
        self.assertEqual(record.result.diagnostics.validation_attempts, (False, True))
        self.assertEqual(len(worker.requests), 2)

    async def test_self_test_is_explicitly_not_live_value_evidence(self) -> None:
        record = await run_firm_value_v2_self_test()

        self.assertTrue(record.passed)
        self.assertEqual(record.provider_calls, 0)
        self.assertFalse(record.quota_consumed)
        self.assertIn("not-live-value-evidence", record.evidence_class)

    async def test_live_envelope_requires_risk_confirmation_and_round_trips(self) -> None:
        config = LiveFirmValueV2Config(
            command="fixture-codex",
            model="fixture-model",
            source_revision="snapshot-sha256:" + "a" * 64,
            distribution_sha256="b" * 64,
            quota_confirmed=True,
            evaluator_risk_confirmed=False,
        )
        with self.assertRaisesRegex(ValueError, "evaluator-risk confirmation"):
            await run_live_firm_value_v2_evaluation(
                config,
                FirmValueV2FixtureKind.SOLO_EDIT,
                CodingStrategyKind.SOLO,
            )

        plan = _plan(FirmValueV2FixtureKind.SOLO_EDIT, CodingStrategyKind.SOLO)
        record = await run_live_firm_value_v2_evaluation(
            replace(config, evaluator_risk_confirmed=True),
            FirmValueV2FixtureKind.SOLO_EDIT,
            CodingStrategyKind.SOLO,
            provider_factory=lambda provider_config: _V2Provider(plan, count_compiler=False),
            coding_worker_factory=lambda provider_config: _V2Worker(
                FirmValueV2FixtureKind.SOLO_EDIT, CodingStrategyKind.SOLO
            ),
        )
        loaded = load_live_firm_value_v2_record(
            json.loads(firm_value_v2_to_json(record))
        )

        self.assertEqual(loaded, record)
        self.assertEqual(record.result.evidence_class, FIRM_VALUE_V2_LIVE_EVIDENCE_CLASS)
        self.assertTrue(record.quota_confirmed)
        self.assertTrue(record.evaluator_risk_confirmed)


class FirmValueV2CliTests(unittest.TestCase):
    def test_cli_exposes_stable_v2_contract(self) -> None:
        output = io.StringIO()

        code = main(["eval", "firm-value-v2"], stdout=output)

        self.assertEqual(code, EXIT_OK)
        rendered = output.getvalue()
        self.assertIn("Firm Value v2: PASS", rendered)
        self.assertIn("offline contract only, not live value evidence", rendered)

    def test_cli_json_has_v2_schema(self) -> None:
        output = io.StringIO()

        code = main(["eval", "firm-value-v2", "--json"], stdout=output)

        self.assertEqual(code, EXIT_OK)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema_version"], "noruct.firm-value-self-test.v2")
        self.assertEqual(payload["report"]["schema_version"], "noruct.firm-value-report.v2")
        self.assertEqual(payload["provider_calls"], 0)


if __name__ == "__main__":
    unittest.main()
