from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

from dynamic_firm import __version__
from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, main
from dynamic_firm.evaluation.workflow_patch_campaign import (
    WorkflowPatchCohortState,
    apply_workflow_patch_cohort,
    approve_workflow_patch_cohort,
    compare_workflow_patch_cohort,
    prepare_workflow_patch_cohort,
    preview_workflow_patch_cohort,
    rollback_workflow_patch_cohort,
    run_next_workflow_patch_cohort_slot,
    workflow_patch_cohort_status,
)
from dynamic_firm.evaluation.workflow_patch_live import (
    WORKFLOW_PATCH_QUALITY_GAIN_THRESHOLD,
    load_live_workflow_patch_record,
    workflow_patch_pattern_id,
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
    (root / "src" / "runtime.py").write_text("VALUE = 51\n", encoding="utf-8")
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


class _WorkflowPatchProvider:
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
        if '"task_id":"specialist_release_policy_review"' in prompt or (
            '"task_id":"policy_evidence"' in prompt
        ):
            completion = CompletionEnvelope(
                summary=(
                    "disposition: RELEASE\n"
                    "policy_basis: attestation-green-rule-r2\n"
                    "required_action: publish-release-notes\n"
                )
            )
        elif '"task_id":"independent_review"' in prompt:
            completion = CompletionEnvelope(
                summary="audit_basis: provenance-audit-green-r4\n"
            )
        elif '"task_id":"integrate_decision"' in prompt:
            completion = CompletionEnvelope(
                summary=(
                    "disposition: RELEASE\n"
                    "public_basis: tests-128-passed\n"
                    "policy_basis: attestation-green-rule-r2\n"
                    "audit_basis: provenance-audit-green-r4\n"
                    "required_action: publish-release-notes\n"
                )
            )
        elif '"task_id":"integrate_goal"' in prompt:
            completion = CompletionEnvelope(
                summary=(
                    "disposition: RELEASE\n"
                    "public_basis: tests-128-passed\n"
                    "policy_basis: attestation-green-rule-r2\n"
                    "audit_basis: unavailable\n"
                    "required_action: publish-release-notes\n"
                )
            )
        else:
            completion = CompletionEnvelope(
                summary=(
                    "disposition: HOLD\n"
                    "public_basis: tests-128-passed\n"
                    "policy_basis: unavailable\n"
                    "audit_basis: unavailable\n"
                    "required_action: policy-review-required\n"
                ),
                signals=(
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "release_policy_review",
                        ("sealed policy requires a dedicated reviewer",),
                    ),
                ),
            )
        return ModelResponse(
            completion=completion,
            usage=Usage(input_tokens=80, output_tokens=20),
            provider_request_id=f"raw-workflow-patch-request-{self.calls}",
        )


async def _prepare(root: Path) -> Path:
    source = _write_source_root(root / "source")
    wheel = _write_wheel(root / f"noruct-{__version__}-py3-none-any.whl")
    cohort = root / "cohort"
    prepared = await prepare_workflow_patch_cohort(
        cohort,
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
    if not prepared.preflight.ready:
        raise AssertionError("fixture preflight must be ready")
    return cohort


class WorkflowPatchCohortTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_is_provider_free_and_requires_one_slot_confirmation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cohort = await _prepare(Path(directory))
            status = workflow_patch_cohort_status(cohort)
            with self.assertRaisesRegex(ValueError, "confirm-live-quota"):
                await run_next_workflow_patch_cohort_slot(
                    cohort,
                    confirm_live_quota=False,
                )

        self.assertEqual(status.state, WorkflowPatchCohortState.READY)
        self.assertEqual(status.completed_runs, 0)
        self.assertEqual(status.expected_runs, 4)
        self.assertEqual(status.external_model_calls_recorded, 0)
        self.assertEqual(status.next_strategy, "generic-post-gap")

    async def test_four_record_cohort_applies_compares_and_rolls_back(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cohort = await _prepare(Path(directory))
            provider = lambda config: _WorkflowPatchProvider()

            baseline = await run_next_workflow_patch_cohort_slot(
                cohort,
                confirm_live_quota=True,
                provider_factory=provider,
            )
            observation_one = await run_next_workflow_patch_cohort_slot(
                cohort,
                confirm_live_quota=True,
                provider_factory=provider,
            )
            observation_two = await run_next_workflow_patch_cohort_slot(
                cohort,
                confirm_live_quota=True,
                provider_factory=provider,
            )

            status = workflow_patch_cohort_status(cohort)
            candidate = preview_workflow_patch_cohort(cohort)
            approved = approve_workflow_patch_cohort(
                cohort,
                confirm=True,
                actor="test:operator",
            )
            applied = apply_workflow_patch_cohort(
                cohort,
                confirm=True,
                actor="test:operator",
            )
            patched = await run_next_workflow_patch_cohort_slot(
                cohort,
                confirm_live_quota=True,
                provider_factory=provider,
            )
            comparison = compare_workflow_patch_cohort(cohort)
            patched_record = load_live_workflow_patch_record(
                Path(patched.record_path)
            )
            rolled_back = rollback_workflow_patch_cohort(
                cohort,
                confirm=True,
                actor="test:operator",
            )
            rollback_status = workflow_patch_cohort_status(cohort)

        self.assertTrue(baseline.task_success)
        self.assertTrue(observation_one.task_success)
        self.assertTrue(observation_two.task_success)
        self.assertEqual(status.state, WorkflowPatchCohortState.AWAITING_APPROVAL)
        self.assertEqual(candidate.pattern.pattern_id, workflow_patch_pattern_id())
        self.assertTrue(candidate.eligible_for_apply)
        self.assertEqual(approved.status.value, "APPROVED")
        self.assertEqual(applied.status.value, "APPLIED")
        self.assertEqual(patched.status.state, WorkflowPatchCohortState.COMPLETE)
        self.assertEqual(patched_record.artifact.quality_score, 1.0)
        self.assertEqual(
            patched_record.prior_exposed_ids,
            (workflow_patch_pattern_id(),),
        )
        self.assertFalse(patched_record.no_gap_control_exposed)
        self.assertTrue(comparison.cohort_gate_passed)
        self.assertGreaterEqual(
            comparison.artifact_quality_gain,
            WORKFLOW_PATCH_QUALITY_GAIN_THRESHOLD,
        )
        self.assertEqual(comparison.post_apply_observations, 1)
        self.assertEqual(rolled_back.status.value, "ROLLED_BACK")
        self.assertEqual(rollback_status.state, WorkflowPatchCohortState.ROLLED_BACK)


class WorkflowPatchCohortCliTests(unittest.TestCase):
    def test_run_next_requires_explicit_quota_confirmation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()

        code = main(
            [
                "eval",
                "workflow-patch-cohort",
                "run-next",
                "/tmp/not-opened",
            ],
            stdout=output,
            stderr=error,
        )

        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("confirm-live-quota", error.getvalue())

    def test_help_exposes_separate_patch_approval_and_apply(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["eval", "workflow-patch-cohort", "--help"])

        self.assertEqual(raised.exception.code, EXIT_OK)
        self.assertIn("patch-preview", output.getvalue())
        self.assertIn("patch-approve", output.getvalue())
        self.assertIn("patch-apply", output.getvalue())
        self.assertIn("rollback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
