from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from dynamic_firm import __version__
from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, main
from dynamic_firm.company import WorkflowPatchAssessmentDecision
from dynamic_firm.company.models import content_digest
from dynamic_firm.evaluation.workflow_patch_campaign import (
    apply_workflow_patch_cohort,
    approve_workflow_patch_cohort,
    compare_workflow_patch_cohort,
    prepare_workflow_patch_cohort,
    run_next_workflow_patch_cohort_slot,
    workflow_patch_cohort_status,
)
from dynamic_firm.evaluation.workflow_patch_extension import (
    WorkflowPatchExtensionState,
    assess_workflow_patch_extension,
    compare_workflow_patch_extension,
    prepare_workflow_patch_extension,
    rollback_workflow_patch_extension,
    run_next_workflow_patch_extension_slot,
    workflow_patch_extension_status,
)
from dynamic_firm.evaluation.workflow_patch_live import (
    run_live_workflow_patch_evaluation,
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
    (root / "src" / "runtime.py").write_text("VALUE = 52\n", encoding="utf-8")
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


async def _unsafe_live_runner(config, strategy, **kwargs):
    record = await run_live_workflow_patch_evaluation(
        config,
        strategy,
        workflow_priors=kwargs["workflow_priors"],
        prior_source=kwargs["prior_source"],
        provider_factory=lambda provider_config: _WorkflowPatchProvider(),
    )
    unsafe = replace(
        record,
        safety=replace(record.safety, passed=False),
    )
    digest = content_digest(unsafe.content_payload())
    return replace(
        unsafe,
        evidence_id=f"workflow-patch-live-evidence-{digest[:24]}",
        content_hash=digest,
    )


async def _parent(root: Path) -> tuple[Path, Path, Path]:
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
        raise AssertionError("fixture parent preflight must be ready")
    provider = lambda config: _WorkflowPatchProvider()
    for _ in range(3):
        result = await run_next_workflow_patch_cohort_slot(
            cohort,
            confirm_live_quota=True,
            provider_factory=provider,
        )
        if not result.task_success:
            raise AssertionError("fixture parent candidate run failed")
    approve_workflow_patch_cohort(
        cohort,
        confirm=True,
        actor="test:operator",
    )
    apply_workflow_patch_cohort(
        cohort,
        confirm=True,
        actor="test:operator",
    )
    patched = await run_next_workflow_patch_cohort_slot(
        cohort,
        confirm_live_quota=True,
        provider_factory=provider,
    )
    if not patched.task_success or not compare_workflow_patch_cohort(
        cohort
    ).cohort_gate_passed:
        raise AssertionError("fixture parent applied run failed")
    return cohort, source, wheel


class WorkflowPatchExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_observations_keep_without_mutating_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, source, wheel = await _parent(root)
            parent_before = workflow_patch_cohort_status(parent)
            extension = root / "extension"
            prepared = await prepare_workflow_patch_extension(
                parent,
                extension,
                wheel=wheel,
                source_root=source,
                model="fixture-cheap-model",
                command="fixture-codex",
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
            with self.assertRaisesRegex(ValueError, "confirm-live-quota"):
                await run_next_workflow_patch_extension_slot(
                    extension,
                    confirm_live_quota=False,
                )
            provider = lambda config: _WorkflowPatchProvider()
            observation_two = await run_next_workflow_patch_extension_slot(
                extension,
                confirm_live_quota=True,
                provider_factory=provider,
            )
            observation_three = await run_next_workflow_patch_extension_slot(
                extension,
                confirm_live_quota=True,
                provider_factory=provider,
            )
            awaiting = workflow_patch_extension_status(extension)
            assessment = assess_workflow_patch_extension(extension)
            final_status = workflow_patch_extension_status(extension)
            comparison = compare_workflow_patch_extension(extension)
            parent_after = workflow_patch_cohort_status(parent)
            parent_comparison = compare_workflow_patch_cohort(parent)

        self.assertTrue(prepared.preflight.ready)
        self.assertEqual(prepared.status.state, WorkflowPatchExtensionState.READY)
        self.assertTrue(observation_two.task_success)
        self.assertTrue(observation_three.task_success)
        self.assertEqual(
            awaiting.state,
            WorkflowPatchExtensionState.AWAITING_ASSESSMENT,
        )
        self.assertEqual(awaiting.post_apply_observations, 3)
        self.assertEqual(
            assessment.decision,
            WorkflowPatchAssessmentDecision.KEEP,
        )
        self.assertEqual(final_status.state, WorkflowPatchExtensionState.KEEP)
        self.assertEqual(final_status.patch_status, "APPLIED")
        self.assertTrue(comparison.extension_gate_passed)
        self.assertEqual(
            comparison.outcome,
            "WORKFLOW_PATCH_LONG_TERM_KEEP_REPRODUCED",
        )
        self.assertEqual(comparison.mean_artifact_quality, 1.0)
        self.assertEqual(comparison.post_apply_observations, 3)
        self.assertEqual(parent_before.event_count, parent_after.event_count)
        self.assertEqual(parent_before.record_paths, parent_after.record_paths)
        self.assertEqual(parent_comparison.post_apply_observations, 1)

    async def test_third_observation_failure_recommends_but_does_not_auto_rollback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, source, wheel = await _parent(root)
            extension = root / "extension"
            await prepare_workflow_patch_extension(
                parent,
                extension,
                wheel=wheel,
                source_root=source,
                model="fixture-cheap-model",
                command="fixture-codex",
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
            await run_next_workflow_patch_extension_slot(
                extension,
                confirm_live_quota=True,
                provider_factory=lambda config: _WorkflowPatchProvider(),
            )
            failed = await run_next_workflow_patch_extension_slot(
                extension,
                confirm_live_quota=True,
                live_runner=_unsafe_live_runner,
            )
            assessment = assess_workflow_patch_extension(extension)
            recommended = workflow_patch_extension_status(extension)
            with self.assertRaisesRegex(ValueError, "requires --confirm"):
                rollback_workflow_patch_extension(
                    extension,
                    confirm=False,
                    actor="test:operator",
                )
            rolled_back = rollback_workflow_patch_extension(
                extension,
                confirm=True,
                actor="test:operator",
            )
            final_status = workflow_patch_extension_status(extension)

        self.assertTrue(failed.task_success)
        self.assertEqual(
            failed.status.state,
            WorkflowPatchExtensionState.PARTIAL_FAILED,
        )
        self.assertEqual(
            assessment.decision,
            WorkflowPatchAssessmentDecision.ROLLBACK_CANDIDATE,
        )
        self.assertEqual(
            recommended.state,
            WorkflowPatchExtensionState.ROLLBACK_CANDIDATE,
        )
        self.assertEqual(recommended.patch_status, "APPLIED")
        self.assertEqual(rolled_back.status.value, "ROLLED_BACK")
        self.assertEqual(final_status.state, WorkflowPatchExtensionState.ROLLED_BACK)


class WorkflowPatchExtensionCliTests(unittest.TestCase):
    def test_run_next_requires_explicit_quota_confirmation(self) -> None:
        output = io.StringIO()
        error = io.StringIO()

        code = main(
            [
                "eval",
                "workflow-patch-extension",
                "run-next",
                "/tmp/not-opened",
            ],
            stdout=output,
            stderr=error,
        )

        self.assertEqual(code, EXIT_INPUT)
        self.assertIn("confirm-live-quota", error.getvalue())

    def test_help_exposes_assess_compare_and_explicit_rollback(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["eval", "workflow-patch-extension", "--help"])

        self.assertEqual(raised.exception.code, EXIT_OK)
        self.assertIn("prepare", output.getvalue())
        self.assertIn("run-next", output.getvalue())
        self.assertIn("assess", output.getvalue())
        self.assertIn("compare", output.getvalue())
        self.assertIn("rollback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
