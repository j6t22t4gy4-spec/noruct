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
from dynamic_firm.evaluation.release_authorization_campaign import (
    compare_release_authorization_pair,
    prepare_release_authorization_pair,
    release_authorization_pair_status,
    run_next_release_authorization_pair_slot,
)
from dynamic_firm.evaluation.release_authorization_live import (
    RELEASE_AUTHORIZATION_LIVE_RUN_SCHEMA,
    load_live_release_authorization_record,
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
    (root / "src" / "runtime.py").write_text("VALUE = 49\n", encoding="utf-8")
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


class _ReleaseProvider:
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
        if '"task_id":"specialist_release_policy_review"' in prompt:
            completion = CompletionEnvelope(
                summary=(
                    "disposition=RELEASE\n"
                    "policy_basis=attestation-green-rule-r2\n"
                    "required_action=publish-release-notes\n"
                ),
                acceptance_evidence=("release-policy:resolved",),
            )
        elif '"task_id":"integrate_goal"' in prompt:
            completion = CompletionEnvelope(
                summary=(
                    "disposition=RELEASE\n"
                    "public_basis=tests-128-passed\n"
                    "policy_basis=attestation-green-rule-r2\n"
                    "required_action=publish-release-notes\n"
                ),
                acceptance_evidence=("release-authorization:integrated",),
            )
        else:
            completion = CompletionEnvelope(
                summary=(
                    "disposition=HOLD\n"
                    "public_basis=tests-128-passed\n"
                    "policy_basis=unavailable\n"
                    "required_action=policy-review-required\n"
                ),
                acceptance_evidence=("public-release-evidence:tests-128-passed",),
                signals=(
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "release_policy_review",
                        ("sealed release policy is unavailable",),
                    ),
                ),
            )
        return ModelResponse(
            completion=completion,
            usage=Usage(input_tokens=80, output_tokens=20),
            provider_request_id=f"raw-release-provider-request-{self.calls}",
        )


class _BadSoloProvider:
    async def complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        return ModelResponse(
            completion=CompletionEnvelope(
                summary=(
                    "disposition=HOLD\n"
                    "public_basis=unknown\n"
                    "policy_basis=unavailable\n"
                    "required_action=policy-review-required\n"
                ),
                signals=(
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "release_policy_review",
                        ("sealed release policy is unavailable",),
                    ),
                ),
            ),
            usage=Usage(input_tokens=20, output_tokens=10),
            provider_request_id="bad-release-solo-request",
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
        if '"task_id":"specialist_release_policy_review"' in prompt:
            task_id = "specialist"
        elif '"task_id":"integrate_goal"' in prompt:
            task_id = "integrate"
        else:
            task_id = "analyze"
        attempt = self.task_calls.get(task_id, 0) + 1
        self.task_calls[task_id] = attempt
        if attempt == 1:
            completion = CompletionEnvelope(summary="disposition=UNKNOWN\n")
        elif task_id == "specialist":
            completion = CompletionEnvelope(
                summary=(
                    "disposition=RELEASE\n"
                    "policy_basis=attestation-green-rule-r2\n"
                    "required_action=publish-release-notes\n"
                )
            )
        elif task_id == "integrate":
            completion = CompletionEnvelope(
                summary=(
                    "disposition=RELEASE\n"
                    "public_basis=tests-128-passed\n"
                    "policy_basis=attestation-green-rule-r2\n"
                    "required_action=publish-release-notes\n"
                )
            )
        else:
            completion = CompletionEnvelope(
                summary=(
                    "disposition=HOLD\n"
                    "public_basis=tests-128-passed\n"
                    "policy_basis=unavailable\n"
                    "required_action=policy-review-required\n"
                ),
                signals=(
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "release_policy_review",
                        ("sealed release policy is unavailable",),
                    ),
                ),
            )
        return ModelResponse(
            completion=completion,
            usage=Usage(input_tokens=20, output_tokens=10),
            provider_request_id=f"repair-release-{self.calls}",
        )


async def _prepare(root: Path):
    source = _write_source_root(root / "source")
    wheel = _write_wheel(root / f"noruct-{__version__}-py3-none-any.whl")
    return await prepare_release_authorization_pair(
        root / "pair",
        wheel=wheel,
        source_root=source,
        model="fixture-cheap-model",
        command="fixture-codex",
        login_status_factory=lambda command: CodexLoginStatus(
            executable="/fixture/codex",
            installed=True,
            authenticated=True,
        ),
        capability_probe=lambda command: ("/fixture/codex", True, "supported"),
    )


class ReleaseAuthorizationPairTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_runs_provider_free_suite_and_seals_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = await _prepare(root)
            status = release_authorization_pair_status(root / "pair")

        self.assertTrue(prepared.preflight.ready)
        self.assertEqual(prepared.preflight.external_model_calls, 0)
        self.assertFalse(prepared.preflight.quota_consumed)
        self.assertEqual(status.state, CampaignState.READY)
        self.assertEqual(status.expected_runs, 2)
        self.assertEqual(status.max_model_calls_for_next_run, 6)
        self.assertEqual(
            (status.next_fixture, status.next_strategy),
            ("release-authorization", "solo-only-counterfactual"),
        )

    async def test_pair_runs_exact_release_trajectory_and_compares_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            pair = root / "pair"
            with self.assertRaisesRegex(ValueError, "confirm-live-quota"):
                await run_next_release_authorization_pair_slot(
                    pair,
                    confirm_live_quota=False,
                )
            self.assertEqual(release_authorization_pair_status(pair).event_count, 1)

            solo = await run_next_release_authorization_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=lambda config: _ReleaseProvider(),
            )
            self.assertTrue(solo.task_success)
            self.assertEqual(solo.status.state, CampaignState.READY)
            self.assertEqual(
                solo.status.next_strategy,
                "typed-organization-admission",
            )
            persisted = Path(solo.record_path).read_text(encoding="utf-8")
            self.assertNotIn("raw-release-provider-request", persisted)

            dynamic = await run_next_release_authorization_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=lambda config: _ReleaseProvider(),
            )
            comparison = compare_release_authorization_pair(pair)
            record = load_live_release_authorization_record(
                Path(dynamic.record_path)
            )

        self.assertEqual(record.schema_version, RELEASE_AUTHORIZATION_LIVE_RUN_SCHEMA)
        self.assertEqual(dynamic.status.state, CampaignState.COMPLETE)
        self.assertEqual(dynamic.status.external_model_calls_recorded, 4)
        self.assertEqual(record.admission.organization_admission_count, 1)
        self.assertEqual(record.admission.final_graph_version, 2)
        self.assertEqual(
            tuple(item.task_id for item in record.trajectory.attempts),
            (
                "analyze_goal",
                "specialist_release_policy_review",
                "integrate_goal",
            ),
        )
        self.assertTrue(comparison.pair_gate_passed)
        self.assertEqual(comparison.artifact_quality_gain, 0.5)
        self.assertEqual(
            comparison.outcome,
            "REPLICATED_TYPED_INFORMATION_BOUNDARY_VALUE",
        )
        self.assertEqual(comparison.aggregator_provider_calls, 0)
        self.assertFalse(comparison.aggregator_quota_consumed)

    async def test_failed_solo_gate_consumes_no_dynamic_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            pair = root / "pair"
            result = await run_next_release_authorization_pair_slot(
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
                await run_next_release_authorization_pair_slot(
                    pair,
                    confirm_live_quota=True,
                    provider_factory=lambda config: _ReleaseProvider(),
                )
            self.assertEqual(
                release_authorization_pair_status(pair).event_count,
                event_count,
            )

    async def test_six_call_dynamic_repair_budget_is_strategy_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            await _prepare(root)
            pair = root / "pair"
            solo = await run_next_release_authorization_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=lambda config: _RepairingEveryTaskProvider(),
            )
            dynamic = await run_next_release_authorization_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=lambda config: _RepairingEveryTaskProvider(),
            )
            comparison = compare_release_authorization_pair(pair)
            solo_record = load_live_release_authorization_record(
                Path(solo.record_path)
            )
            dynamic_record = load_live_release_authorization_record(
                Path(dynamic.record_path)
            )

        self.assertEqual(solo_record.external_model_calls, 2)
        self.assertEqual(dynamic_record.external_model_calls, 6)
        self.assertTrue(dynamic_record.validation.repair_used)
        self.assertEqual(
            solo_record.identity.workload_hash,
            dynamic_record.identity.workload_hash,
        )
        self.assertTrue(comparison.budget_gate_passed)
        self.assertTrue(comparison.pair_gate_passed)


class ReleaseAuthorizationPairCliTests(unittest.TestCase):
    def test_run_next_requires_explicit_quota_confirmation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()

        code = main(
            [
                "eval",
                "release-authorization-pair",
                "run-next",
                "/tmp/not-opened",
            ],
            stdout=output,
            stderr=error,
        )

        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("confirm-live-quota", error.getvalue())

    def test_help_exposes_release_pair_control_plane(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "eval",
                    "release-authorization-pair",
                    "prepare",
                    "--help",
                ]
            )

        self.assertEqual(raised.exception.code, EXIT_OK)
        self.assertIn("--model", output.getvalue())
        self.assertIn("--max-pair-model-calls", output.getvalue())


if __name__ == "__main__":
    unittest.main()
