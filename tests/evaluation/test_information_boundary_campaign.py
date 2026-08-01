from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

from dynamic_firm import __version__
from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, main
from dynamic_firm.evaluation.firm_value_campaign import CampaignState
from dynamic_firm.evaluation.information_boundary import (
    INFORMATION_BOUNDARY_LIVE_RUN_SCHEMA,
    create_information_boundary_preflight,
    load_live_information_boundary_record,
)
from dynamic_firm.evaluation.information_boundary_campaign import (
    compare_information_boundary_pair,
    information_boundary_pair_status,
    prepare_information_boundary_pair,
    run_next_information_boundary_pair_slot,
)
from dynamic_firm.providers.codex_exec import CodexLoginStatus
from dynamic_firm.runtime.models import (
    CompletionEnvelope,
    ModelRequest,
    ModelResponse,
    RunSignal,
    SignalCode,
    Usage,
)
from dynamic_firm.runtime.ports import CancellationToken


def _write_source_root(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "runtime.py").write_text("VALUE = 45\n", encoding="utf-8")
    (root / "tests" / "test_runtime.py").write_text(
        "def test_value(): pass\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname='noruct'\n",
        encoding="utf-8",
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


class _BoundaryProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        self.calls += 1
        prompt = "\n".join(str(message.content) for message in request.messages)
        if '"task_id":"specialist_sealed_policy_review"' in prompt:
            completion = CompletionEnvelope(
                summary=(
                    "decision=manual-review\n"
                    "sealed_evidence=risk-9-threshold-7\n"
                ),
                acceptance_evidence=("sealed-policy:resolved",),
            )
        elif '"task_id":"integrate_goal"' in prompt:
            completion = CompletionEnvelope(
                summary=(
                    "decision=manual-review\n"
                    "public_evidence=rollback-ready\n"
                    "sealed_evidence=risk-9-threshold-7\n"
                ),
                acceptance_evidence=("information-boundary:integrated",),
            )
        else:
            completion = CompletionEnvelope(
                summary=(
                    "decision=insufficient-evidence\n"
                    "public_evidence=rollback-ready\n"
                    "sealed_evidence=unavailable\n"
                ),
                acceptance_evidence=("public-evidence:rollback-ready",),
                signals=(
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "sealed_policy_review",
                        ("sealed policy memory is not available to this employee",),
                    ),
                ),
            )
        return ModelResponse(
            completion=completion,
            usage=Usage(input_tokens=100, output_tokens=25),
            provider_request_id=f"raw-provider-request-{self.calls}",
        )


class _BadSoloProvider:
    async def complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> ModelResponse:
        return ModelResponse(
            completion=CompletionEnvelope(
                summary=(
                    "decision=insufficient-evidence\n"
                    "public_evidence=unknown\n"
                    "sealed_evidence=unavailable\n"
                ),
                signals=(
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "sealed_policy_review",
                        ("sealed policy is unavailable",),
                    ),
                ),
            ),
            usage=Usage(input_tokens=20, output_tokens=10),
            provider_request_id="bad-solo-request",
        )


class _RepairingSoloProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        self.calls += 1
        if self.calls == 1:
            completion = CompletionEnvelope(
                summary=(
                    "decision=unknown\n"
                    "public_evidence=unknown\n"
                    "sealed_evidence=unknown\n"
                )
            )
        else:
            completion = CompletionEnvelope(
                summary=(
                    "decision=insufficient-evidence\n"
                    "public_evidence=rollback-ready\n"
                    "sealed_evidence=unavailable\n"
                ),
                signals=(
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "sealed_policy_review",
                        ("sealed policy is unavailable",),
                    ),
                ),
            )
        return ModelResponse(
            completion=completion,
            usage=Usage(input_tokens=20, output_tokens=10),
            provider_request_id=f"repairing-solo-{self.calls}",
        )


class _RepairingEveryTaskProvider:
    def __init__(self) -> None:
        self.task_calls: dict[str, int] = {}
        self.calls = 0

    async def complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        self.calls += 1
        prompt = "\n".join(str(message.content) for message in request.messages)
        if '"task_id":"specialist_sealed_policy_review"' in prompt:
            task_id = "specialist"
        elif '"task_id":"integrate_goal"' in prompt:
            task_id = "integrate"
        else:
            task_id = "analyze"
        attempt = self.task_calls.get(task_id, 0) + 1
        self.task_calls[task_id] = attempt
        if attempt == 1:
            completion = CompletionEnvelope(summary="decision=unknown\n")
        elif task_id == "specialist":
            completion = CompletionEnvelope(
                summary=(
                    "decision=manual-review\n"
                    "sealed_evidence=risk-9-threshold-7\n"
                ),
                acceptance_evidence=("sealed-policy:resolved",),
            )
        elif task_id == "integrate":
            completion = CompletionEnvelope(
                summary=(
                    "decision=manual-review\n"
                    "public_evidence=rollback-ready\n"
                    "sealed_evidence=risk-9-threshold-7\n"
                ),
                acceptance_evidence=("information-boundary:integrated",),
            )
        else:
            completion = CompletionEnvelope(
                summary=(
                    "decision=insufficient-evidence\n"
                    "public_evidence=rollback-ready\n"
                    "sealed_evidence=unavailable\n"
                ),
                signals=(
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "sealed_policy_review",
                        ("sealed policy is unavailable",),
                    ),
                ),
            )
        return ModelResponse(
            completion=completion,
            usage=Usage(input_tokens=20, output_tokens=10),
            provider_request_id=f"repair-every-task-{self.calls}",
        )


async def _prepare(root: Path):
    source = _write_source_root(root / "source")
    wheel = _write_wheel(root / f"noruct-{__version__}-py3-none-any.whl")
    phase44 = root / "phase44-preflight.json"
    await create_information_boundary_preflight(
        phase44,
        wheel=wheel,
        source_root=source,
        reserved_model_profile="fixture-cheap-model",
    )
    return await prepare_information_boundary_pair(
        root / "pair",
        preflight=phase44,
        wheel=wheel,
        source_root=source,
        command="fixture-codex",
        login_status_factory=lambda command: CodexLoginStatus(
            executable="/fixture/codex",
            installed=True,
            authenticated=True,
        ),
        capability_probe=lambda command: ("/fixture/codex", True, "supported"),
    )


class InformationBoundaryPairTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_freezes_exact_pair_without_quota(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = await _prepare(root)
            status = information_boundary_pair_status(root / "pair")

        self.assertTrue(prepared.preflight.ready)
        self.assertEqual(prepared.preflight.external_model_calls, 0)
        self.assertFalse(prepared.preflight.quota_consumed)
        self.assertEqual(status.state, CampaignState.READY)
        self.assertEqual(status.expected_runs, 2)
        self.assertEqual(status.max_model_calls_for_next_run, 6)
        self.assertEqual(
            (status.next_fixture, status.next_strategy),
            ("typed-information-boundary", "solo-only-counterfactual"),
        )

    async def test_pair_runs_solo_then_admission_and_compares_provider_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            pair = root / "pair"
            with self.assertRaisesRegex(ValueError, "confirm-live-quota"):
                await run_next_information_boundary_pair_slot(
                    pair,
                    confirm_live_quota=False,
                )
            self.assertEqual(information_boundary_pair_status(pair).event_count, 1)

            solo = await run_next_information_boundary_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=lambda config: _BoundaryProvider(),
            )
            self.assertTrue(solo.task_success)
            self.assertEqual(solo.status.state, CampaignState.READY)
            self.assertEqual(
                solo.status.next_strategy,
                "typed-organization-admission",
            )
            self.assertNotIn(
                "raw-provider-request",
                Path(solo.record_path).read_text(encoding="utf-8"),
            )

            dynamic = await run_next_information_boundary_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=lambda config: _BoundaryProvider(),
            )
            before = dynamic.status
            comparison = compare_information_boundary_pair(pair)
            after = information_boundary_pair_status(pair)

        self.assertEqual(before.state, CampaignState.COMPLETE)
        self.assertEqual(before.completed_runs, 2)
        self.assertEqual(before.external_model_calls_recorded, 4)
        self.assertTrue(comparison.pair_gate_passed)
        self.assertEqual(comparison.artifact_quality_gain, 0.4)
        self.assertEqual(
            comparison.outcome,
            "INFORMATION_BOUNDARY_VALUE_OBSERVED",
        )
        self.assertEqual(comparison.aggregator_provider_calls, 0)
        self.assertFalse(comparison.aggregator_quota_consumed)
        self.assertEqual(after.event_count, before.event_count + 1)

    async def test_failed_solo_gate_stops_before_second_quota_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            pair = root / "pair"
            result = await run_next_information_boundary_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=lambda config: _BadSoloProvider(),
            )
            event_count = result.status.event_count

            self.assertEqual(result.status.state, CampaignState.PARTIAL_FAILED)
            self.assertEqual(
                result.status.stop_reason,
                "SOLO_COMPLETION_VALIDATION_FAILED",
            )
            self.assertIsNone(result.status.next_strategy)
            with self.assertRaisesRegex(ValueError, "state=PARTIAL_FAILED"):
                await run_next_information_boundary_pair_slot(
                    pair,
                    confirm_live_quota=True,
                    provider_factory=lambda config: _BoundaryProvider(),
                )
            self.assertEqual(
                information_boundary_pair_status(pair).event_count,
                event_count,
            )

    async def test_solo_completion_contract_repairs_once_and_seals_v4_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            pair = root / "pair"
            result = await run_next_information_boundary_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=lambda config: _RepairingSoloProvider(),
            )
            record_path = Path(result.record_path)
            record = load_live_information_boundary_record(record_path)
            persisted = record_path.read_text(encoding="utf-8")

        self.assertEqual(record.schema_version, INFORMATION_BOUNDARY_LIVE_RUN_SCHEMA)
        self.assertTrue(record.task_success)
        self.assertTrue(record.validation.passed)
        self.assertTrue(record.validation.repair_used)
        self.assertEqual(record.validation.attempt_count, 2)
        self.assertEqual(
            record.validation.failed_checks,
            (
                "capability-signal",
                "decision",
                "public-evidence",
                "sealed-evidence",
            ),
        )
        self.assertEqual(record.external_model_calls, 2)
        self.assertEqual(record.admission.organization_admission_count, 0)
        self.assertEqual(record.admission.attempt_count, 1)
        self.assertEqual(record.trajectory.task_mutation_count, 0)
        self.assertEqual(record.trajectory.graph_patch_count, 0)
        self.assertEqual(result.status.state, CampaignState.READY)
        self.assertEqual(
            result.status.next_strategy,
            "typed-organization-admission",
        )
        self.assertNotIn("decision=unknown", persisted)
        self.assertNotIn("sealed policy is unavailable", persisted)

    async def test_six_call_dynamic_repair_budget_preserves_same_workload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            pair = root / "pair"
            solo = await run_next_information_boundary_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=lambda config: _RepairingEveryTaskProvider(),
            )
            dynamic = await run_next_information_boundary_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=lambda config: _RepairingEveryTaskProvider(),
            )
            comparison = compare_information_boundary_pair(pair)
            dynamic_record = load_live_information_boundary_record(
                Path(dynamic.record_path)
            )

        self.assertTrue(solo.task_success)
        self.assertTrue(dynamic.task_success)
        self.assertEqual(dynamic_record.external_model_calls, 6)
        self.assertTrue(dynamic_record.validation.repair_used)
        self.assertEqual(dynamic_record.admission.organization_admission_count, 1)
        self.assertEqual(dynamic.status.external_model_calls_recorded, 8)
        self.assertTrue(comparison.budget_gate_passed)
        self.assertTrue(comparison.pair_gate_passed)
        self.assertEqual(comparison.artifact_quality_gain, 0.4)


class InformationBoundaryPairCliTests(unittest.TestCase):
    def test_run_next_requires_explicit_quota_confirmation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()

        code = main(
            [
                "eval",
                "information-boundary-pair",
                "run-next",
                "/tmp/not-opened",
            ],
            stdout=output,
            stderr=error,
        )

        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("confirm-live-quota", error.getvalue())

    def test_help_exposes_distinct_pair_control_plane(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["eval", "information-boundary-pair", "run-next", "--help"])

        self.assertEqual(raised.exception.code, EXIT_OK)
        self.assertIn("--confirm-live-quota", output.getvalue())


if __name__ == "__main__":
    unittest.main()
