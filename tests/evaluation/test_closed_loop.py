from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.coding import CodingWorkResult
from dynamic_firm.evaluation.closed_loop import (
    CodingStrategyKind,
    LiveCodingEvaluationConfig,
    closed_loop_records_to_json,
    run_closed_loop_evaluation,
    run_closed_loop_matrix,
    run_live_coding_preflight,
    run_live_coding_evaluation,
)
from dynamic_firm.evaluation.coding import CodingFixtureKind
from dynamic_firm.providers.codex_exec import CodexLoginStatus
from dynamic_firm.runtime.models import StructuredOutputResponse, Usage


PROJECT_ROOT = Path(__file__).parents[2]


class LiveSoloWorker:
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
            summary="Live fixture worker prepared the bounded change.",
            usage=Usage(
                model_calls=1,
                input_tokens=113_031,
                cached_input_tokens=83_968,
                output_tokens=9,
            ),
        )


class LiveRecoveryWorker:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request, cancellation):
        cancellation.raise_if_cancelled()
        self.requests.append(request)
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
            summary="Live recovery worker prepared one bounded candidate.",
            usage=Usage(input_tokens=17, output_tokens=9),
            provider_request_id=f"live-recovery-{len(self.requests)}",
        )


class UnusedLiveProvider:
    async def complete(self, request, cancellation):
        raise AssertionError("The solo counterfactual should not call a native evidence employee")


class LiveDynamicProvider(UnusedLiveProvider):
    async def complete_structured(self, request, cancellation):
        cancellation.raise_if_cancelled()
        return StructuredOutputResponse(
            value={
                "mode": "SOLO",
                "rationale": "One implementation employee is sufficient.",
                "assumptions": [],
                "tasks": [
                    {
                        "task_id": "implement_change",
                        "objective": "Implement the bounded fixture correction.",
                        "depends_on": [],
                        "required_capabilities": ["implementation"],
                        "acceptance_criteria": ["The fixture validator passes."],
                        "risk_level": "LOW",
                    }
                ],
                "final_task_id": "implement_change",
            },
            usage=Usage(input_tokens=11, output_tokens=7),
            provider_request_id="live-compiler-fixture",
        )


class ClosedLoopCodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_recovery_adapter_reenters_the_same_shadow_once(self) -> None:
        worker = LiveRecoveryWorker()

        record = await run_live_coding_evaluation(
            LiveCodingEvaluationConfig(
                command="fake-codex",
                model="fixture-model",
                source_revision="fixture-revision",
                max_total_model_calls=4,
            ),
            CodingFixtureKind.TEST_GUIDED_RECOVERY,
            CodingStrategyKind.SOLO,
            provider_factory=lambda config: UnusedLiveProvider(),
            coding_worker_factory=lambda config: worker,
        )

        self.assertEqual(record.result.status.value, "SUCCEEDED")
        self.assertEqual(record.external_model_calls, 2)
        self.assertEqual(record.validation_observation_scope, "noruct-bounded-recovery-handshake")
        self.assertEqual(record.result.trajectory.validation_attempts, (False, True))
        self.assertEqual(len(worker.requests), 2)
        self.assertEqual(worker.requests[0].workspace, worker.requests[1].workspace)
        self.assertEqual(worker.requests[0].validation_feedback, ())
        self.assertFalse(worker.requests[1].validation_feedback[0].passed)
        self.assertEqual(
            worker.requests[1].validation_feedback[0].detail,
            "failed:reversed-bounds "
            "expect:raise-ValueError-when-lower-greater-than-upper",
        )

    async def test_parallel_live_preflight_rehearses_contract_without_quota(self) -> None:
        record = await run_live_coding_preflight(
            LiveCodingEvaluationConfig(
                command="/opt/fixture/codex",
                model="fixture-model",
                source_revision="fixture-revision",
                max_total_model_calls=4,
            ),
            CodingFixtureKind.PARALLEL_EVIDENCE,
            CodingStrategyKind.DYNAMIC,
            login_status_factory=lambda command: CodexLoginStatus(
                executable=command,
                installed=True,
                authenticated=True,
            ),
        )

        self.assertTrue(record.ready)
        self.assertFalse(record.quota_consumed)
        self.assertEqual(record.external_model_calls, 0)
        self.assertEqual(record.evidence_class, "readiness-only-not-live-evidence")
        self.assertEqual(record.offline_rehearsal.trajectory.employee_count, 2)
        self.assertEqual(record.offline_rehearsal.trajectory.maximum_parallelism, 2)
        self.assertTrue(all(check.passed for check in record.checks))

    async def test_parallel_live_preflight_blocks_an_implicit_model(self) -> None:
        record = await run_live_coding_preflight(
            LiveCodingEvaluationConfig(
                command="/opt/fixture/codex",
                source_revision="fixture-revision",
                max_total_model_calls=4,
            ),
            CodingFixtureKind.PARALLEL_EVIDENCE,
            CodingStrategyKind.DYNAMIC,
            login_status_factory=lambda command: CodexLoginStatus(
                executable=command,
                installed=True,
                authenticated=True,
            ),
        )

        self.assertFalse(record.ready)
        checks = {check.name: check for check in record.checks}
        self.assertFalse(checks["model-id-explicit"].passed)
        self.assertEqual(record.model_id, "missing")

    async def test_live_dynamic_strategy_uses_the_real_compiler_surface(self) -> None:
        record = await run_live_coding_evaluation(
            LiveCodingEvaluationConfig(
                command="fake-codex",
                model="fixture-model",
                source_revision="fixture-revision",
            ),
            CodingFixtureKind.SOLO_EDIT,
            CodingStrategyKind.DYNAMIC,
            provider_factory=lambda config: LiveDynamicProvider(),
            coding_worker_factory=lambda config: LiveSoloWorker(),
        )

        self.assertEqual(record.planner_source, "live-dynamic-workflow-compiler")
        self.assertEqual(record.external_model_calls, 2)
        self.assertEqual(record.result.planning_mode, "SOLO")
        self.assertEqual(record.result.compiler_model_calls, 1)
        self.assertTrue(record.result.score.task_success)

    async def test_live_evaluation_requires_an_explicit_model_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit model id"):
            await run_live_coding_evaluation(
                LiveCodingEvaluationConfig(
                    command="fake-codex",
                    source_revision="fixture-revision",
                ),
                CodingFixtureKind.SOLO_EDIT,
                CodingStrategyKind.SOLO,
                provider_factory=lambda config: UnusedLiveProvider(),
                coding_worker_factory=lambda config: LiveSoloWorker(),
            )

    async def test_dynamic_parallel_run_derives_staffing_writer_and_authority_from_ledger(self) -> None:
        record = await run_closed_loop_evaluation(
            CodingFixtureKind.PARALLEL_EVIDENCE,
            CodingStrategyKind.DYNAMIC,
        )

        self.assertEqual(record.status.value, "SUCCEEDED")
        self.assertEqual(record.planning_mode, "DYNAMIC")
        self.assertEqual(record.trajectory_source, "append-only-runtime-ledger")
        self.assertEqual(record.trajectory.employee_count, 2)
        self.assertEqual(record.trajectory.maximum_parallelism, 2)
        self.assertEqual(record.trajectory.writer_employee_ids, ("employee-dynamic-writer",))
        self.assertEqual(record.trajectory.approvals_requested, 1)
        self.assertEqual(record.trajectory.approvals_granted, 1)
        self.assertEqual(record.trajectory.preapproval_workspace_mutations, 0)
        self.assertTrue(record.workspace_unchanged_before_approval)
        self.assertTrue(record.ledger_matches_kernel)
        self.assertTrue(record.score.overall_passed)

    async def test_recovery_attempts_are_actual_worker_validations_replayed_from_ledger(self) -> None:
        record = await run_closed_loop_evaluation(
            CodingFixtureKind.TEST_GUIDED_RECOVERY,
            CodingStrategyKind.DYNAMIC,
        )

        self.assertEqual(record.trajectory.validation_attempts, (False, True))
        self.assertTrue(record.score.recovery_correctness)
        self.assertTrue(record.score.validation_passed)
        self.assertTrue(record.score.task_success)

    async def test_three_by_three_matrix_exposes_counterfactual_cost_without_faking_task_failure(self) -> None:
        records = await run_closed_loop_matrix()
        by_key = {(record.fixture, record.strategy): record for record in records}

        self.assertEqual(len(records), 9)
        self.assertTrue(all(record.score.task_success for record in records))
        self.assertEqual(
            by_key[(CodingFixtureKind.SOLO_EDIT, CodingStrategyKind.FIXED)].trajectory.employee_count,
            3,
        )
        self.assertEqual(
            by_key[(CodingFixtureKind.PARALLEL_EVIDENCE, CodingStrategyKind.SOLO)].score.quality_score,
            0.6667,
        )
        self.assertTrue(
            by_key[(CodingFixtureKind.PARALLEL_EVIDENCE, CodingStrategyKind.DYNAMIC)].score.overall_passed
        )
        snapshot = json.loads(
            (
                PROJECT_ROOT
                / "tests"
                / "fixtures"
                / "public_evaluation"
                / "closed-loop-matrix.json"
            ).read_text(encoding="utf-8")
        )
        actual = [
            {
                "fixture": record.fixture.value,
                "strategy": record.strategy.value,
                "status": record.status.value,
                "planning_mode": record.planning_mode,
                "runs": record.ledger_run_count,
                "events": record.ledger_event_count,
                "employees": record.trajectory.employee_count,
                "parallelism": record.trajectory.maximum_parallelism,
                "writers": len(record.trajectory.writer_employee_ids),
                "approvals": (
                    f"{record.trajectory.approvals_granted}/"
                    f"{record.trajectory.approvals_requested}"
                ),
                "preapproval_mutations": record.trajectory.preapproval_workspace_mutations,
                "validation_attempts": list(record.trajectory.validation_attempts),
                "task_success": record.score.task_success,
                "overall_passed": record.score.overall_passed,
                "quality_score": record.score.quality_score,
            }
            for record in records
        ]
        self.assertEqual(snapshot["records"], actual)

    async def test_record_json_excludes_ephemeral_ids_paths_and_timestamps(self) -> None:
        first = await run_closed_loop_evaluation("solo-edit", "dynamic")
        second = await run_closed_loop_evaluation("solo-edit", "dynamic")

        first_json = closed_loop_records_to_json((first,))
        second_json = closed_loop_records_to_json((second,))
        self.assertEqual(first_json, second_json)
        self.assertNotIn("noruct-closed-loop-", first_json)
        self.assertNotIn("runtime.db", first_json)


class ClosedLoopCodingCliTests(unittest.TestCase):
    def test_eval_cli_is_config_and_credentials_independent(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            invalid_config = Path(directory) / "invalid.toml"
            invalid_config.write_text("this is not toml = [", encoding="utf-8")
            exit_code = main(
                [
                    "--config",
                    str(invalid_config),
                    "eval",
                    "coding",
                    "solo-edit",
                    "--strategy",
                    "dynamic",
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["trajectory_source"], "append-only-runtime-ledger")
        self.assertTrue(payload[0]["score"]["overall_passed"])
        self.assertEqual(error.getvalue(), "")

    def test_live_cli_requires_explicit_confirmation_single_case_and_record_path(self) -> None:
        cases = (
            ["eval", "coding", "solo-edit", "--strategy", "solo", "--live"],
            [
                "eval",
                "coding",
                "all",
                "--strategy",
                "solo",
                "--live",
                "--confirm-live-quota",
                "--output",
                "record.json",
            ],
            [
                "eval",
                "coding",
                "solo-edit",
                "--strategy",
                "solo",
                "--live",
                "--confirm-live-quota",
            ],
        )
        expected = (
            "requires --confirm-live-quota",
            "requires exactly one fixture and one strategy",
            "requires --output",
        )
        for argv, message in zip(cases, expected, strict=True):
            with self.subTest(argv=argv):
                error = io.StringIO()
                exit_code = main(argv, stdout=io.StringIO(), stderr=error)
                self.assertEqual(exit_code, 2)
                self.assertIn(message, error.getvalue())

    def test_parallel_preflight_writes_readiness_record_without_live_confirmation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            record_path = Path(directory) / "parallel-preflight.json"
            with patch(
                "dynamic_firm.providers.codex_exec.CodexExecProvider.login_status",
                return_value=CodexLoginStatus(
                    executable="/opt/fixture/codex",
                    installed=True,
                    authenticated=True,
                ),
            ):
                exit_code = main(
                    [
                        "eval",
                        "coding",
                        "parallel-evidence",
                        "--strategy",
                        "dynamic",
                        "--preflight-live",
                        "--codex-command",
                        "/opt/fixture/codex",
                        "--source-revision",
                        "fixture-revision",
                        "--model",
                        "fixture-model",
                        "--output",
                        str(record_path),
                        "--json",
                    ],
                    stdout=output,
                    stderr=error,
                )
            persisted = json.loads(record_path.read_text(encoding="utf-8"))
            mode = record_path.stat().st_mode & 0o777

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(json.loads(output.getvalue()), persisted)
        self.assertEqual(persisted["schema_version"], "noruct.live-coding-preflight.v1")
        self.assertTrue(persisted["ready"])
        self.assertFalse(persisted["quota_consumed"])
        self.assertEqual(persisted["external_model_calls"], 0)
        self.assertEqual(mode, 0o600)

    def test_parallel_preflight_rejects_wrong_case_before_provider_access(self) -> None:
        error = io.StringIO()
        exit_code = main(
            [
                "eval",
                "coding",
                "solo-edit",
                "--strategy",
                "dynamic",
                "--preflight-live",
                "--output",
                "unused.json",
            ],
            stdout=io.StringIO(),
            stderr=error,
        )

        self.assertEqual(exit_code, 2)
        self.assertIn("requires parallel-evidence", error.getvalue())

    def test_confirmed_live_cli_records_bounded_fake_codex_evidence(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            record_path = Path(directory) / "live-record.json"
            exit_code = main(
                [
                    "eval",
                    "coding",
                    "solo-edit",
                    "--strategy",
                    "solo",
                    "--live",
                    "--confirm-live-quota",
                    "--codex-command",
                    "fake-codex",
                    "--model",
                    "fixture-model",
                    "--source-revision",
                    "fixture-revision",
                    "--output",
                    str(record_path),
                    "--json",
                ],
                provider_factory=lambda config: UnusedLiveProvider(),
                coding_worker_factory=lambda config: LiveSoloWorker(),
                stdout=output,
                stderr=error,
            )
            persisted = json.loads(record_path.read_text(encoding="utf-8"))
            mode = record_path.stat().st_mode & 0o777

        printed = json.loads(output.getvalue())
        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual(printed, persisted)
        self.assertEqual(persisted["schema_version"], "noruct.live-coding-evaluation.v3")
        self.assertTrue(persisted["quota_confirmed"])
        self.assertTrue(persisted["evidence_id"].startswith("live-evidence-"))
        self.assertTrue(persisted["evaluation_run_id"].startswith("live-run-"))
        self.assertEqual(len(persisted["content_hash"]), 64)
        self.assertEqual(persisted["source_revision"], "fixture-revision")
        self.assertEqual(persisted["model_id"], "fixture-model")
        self.assertEqual(persisted["planner_source"], "bounded-counterfactual-plan")
        self.assertIsNone(persisted["subscription_cost_usd"])
        self.assertEqual(persisted["external_model_calls"], 1)
        self.assertEqual(persisted["result"]["trajectory"]["validation_attempts"], [True])
        self.assertTrue(persisted["result"]["score"]["task_success"])
        self.assertEqual(mode, 0o600)
        self.assertEqual(error.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
