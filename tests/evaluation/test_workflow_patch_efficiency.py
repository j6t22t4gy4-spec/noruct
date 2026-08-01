from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, build_parser
from dynamic_firm.evaluation.workflow_patch_efficiency import (
    WorkflowPatchEfficiencyState,
    compare_workflow_patch_efficiency_pair,
    create_workflow_patch_exact_context_binding,
    evaluate_workflow_patch_natural_preflight,
    prepare_workflow_patch_exact_context_evaluation,
    prepare_workflow_patch_efficiency_pair,
    run_next_workflow_patch_efficiency_slot,
    workflow_patch_efficiency_status,
)
from dynamic_firm.evaluation.workflow_patch_live import (
    WORKFLOW_PATCH_COMPLETION_CONTRACT_V2_REVISION,
    WORKFLOW_PATCH_COMPLETION_CONTRACT_V3_REVISION,
)
from dynamic_firm.evaluation.workflow_patch_extension import (
    assess_workflow_patch_extension,
    compare_workflow_patch_extension,
    prepare_workflow_patch_extension,
    run_next_workflow_patch_extension_slot,
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

from tests.evaluation.test_workflow_patch_extension import (
    _WorkflowPatchProvider,
    _parent,
)


class _EfficiencyProvider:
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
        candidate = "workflow-patch-task-local" in prompt
        repair = "COMPLETION_VALIDATION_FAILED" in prompt
        if '"task_id":"policy_evidence"' in prompt:
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
                    (
                        "disposition: RELEASE\n"
                        "public_basis: tests-128-passed\n"
                        "policy_basis: attestation-green-rule-r2\n"
                        "audit_basis: provenance-audit-green-r4\n"
                        "required_action: publish-release-notes\n"
                    )
                    if candidate or repair
                    else "audit_basis: provenance-audit-green-r4\n"
                )
            )
        else:
            completion = CompletionEnvelope(
                summary=(
                    (
                        "disposition: HOLD\n"
                        "public_basis: tests-128-passed\n"
                        "policy_basis: unavailable\n"
                        "audit_basis: unavailable\n"
                        "required_action: policy-review-required\n"
                    )
                    if candidate or repair
                    else (
                        "disposition: HOLD\n"
                        "public_basis: tests-128-passed\n"
                        "policy_basis: unavailable\n"
                        "required_action: policy-review-required\n"
                    )
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
            provider_request_id=f"efficiency-request-{self.calls}",
        )


async def _keep_extension(root: Path) -> tuple[Path, Path, Path]:
    parent, source, wheel = await _parent(root)
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
    if not prepared.preflight.ready:
        raise AssertionError("fixture extension preflight must be ready")
    provider = lambda config: _WorkflowPatchProvider()
    for _ in range(2):
        result = await run_next_workflow_patch_extension_slot(
            extension,
            confirm_live_quota=True,
            provider_factory=provider,
        )
        if not result.task_success:
            raise AssertionError("fixture extension run failed")
    assess_workflow_patch_extension(extension)
    comparison = compare_workflow_patch_extension(extension)
    if not comparison.extension_gate_passed:
        raise AssertionError("fixture extension comparison failed")
    return extension, source, wheel


class WorkflowPatchEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_frozen_pair_reduces_repairs_without_quality_loss(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, source, wheel = await _keep_extension(root)
            pair = root / "efficiency"
            prepared = await prepare_workflow_patch_efficiency_pair(
                parent,
                pair,
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
                await run_next_workflow_patch_efficiency_slot(
                    pair,
                    confirm_live_quota=False,
                )
            provider = lambda config: _EfficiencyProvider()
            control = await run_next_workflow_patch_efficiency_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=provider,
            )
            candidate = await run_next_workflow_patch_efficiency_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=provider,
            )
            report = compare_workflow_patch_efficiency_pair(pair)
            final_status = workflow_patch_efficiency_status(pair)

        self.assertTrue(prepared.preflight.ready)
        self.assertEqual(
            prepared.status.state,
            WorkflowPatchEfficiencyState.READY,
        )
        self.assertTrue(control.task_success)
        self.assertTrue(candidate.task_success)
        self.assertEqual(final_status.state, WorkflowPatchEfficiencyState.COMPLETE)
        self.assertTrue(report.pair_gate_passed)
        self.assertTrue(report.target_call_bound_met)
        self.assertEqual(report.control_quality, 1.0)
        self.assertEqual(report.candidate_quality, 1.0)
        self.assertEqual(report.control_model_calls, 6)
        self.assertEqual(report.candidate_model_calls, 4)
        self.assertEqual(report.control_repairs, 2)
        self.assertEqual(report.candidate_repairs, 0)
        self.assertEqual(
            report.outcome,
            "COMPLETION_EFFICIENCY_TARGET_OBSERVED",
        )
        self.assertEqual(
            report.recommended_direction,
            "run-natural-workload-observation",
        )

    async def test_system_task_projection_removes_global_contract_ambiguity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, source, wheel = await _keep_extension(root)
            pair = root / "efficiency-v2"
            prepared = await prepare_workflow_patch_efficiency_pair(
                parent,
                pair,
                wheel=wheel,
                source_root=source,
                model="fixture-cheap-model",
                command="fixture-codex",
                completion_contract_revision=(
                    WORKFLOW_PATCH_COMPLETION_CONTRACT_V2_REVISION
                ),
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
            provider = lambda config: _EfficiencyProvider()
            for _ in range(2):
                result = await run_next_workflow_patch_efficiency_slot(
                    pair,
                    confirm_live_quota=True,
                    provider_factory=provider,
                )
                self.assertTrue(result.task_success)
            report = compare_workflow_patch_efficiency_pair(pair)

        self.assertTrue(prepared.preflight.ready)
        self.assertTrue(report.pair_gate_passed)
        self.assertEqual(report.control_model_calls, 6)
        self.assertEqual(report.candidate_model_calls, 4)
        self.assertEqual(report.candidate_repairs, 0)

    async def test_task_objective_projection_makes_no_signal_explicit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, source, wheel = await _keep_extension(root)
            pair = root / "efficiency-v3"
            await prepare_workflow_patch_efficiency_pair(
                parent,
                pair,
                wheel=wheel,
                source_root=source,
                model="fixture-cheap-model",
                command="fixture-codex",
                completion_contract_revision=(
                    WORKFLOW_PATCH_COMPLETION_CONTRACT_V3_REVISION
                ),
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
            provider = lambda config: _EfficiencyProvider()
            for _ in range(2):
                await run_next_workflow_patch_efficiency_slot(
                    pair,
                    confirm_live_quota=True,
                    provider_factory=provider,
                )
            report = compare_workflow_patch_efficiency_pair(pair)

        self.assertTrue(report.pair_gate_passed)
        self.assertEqual(report.candidate_model_calls, 4)
        self.assertEqual(report.candidate_repairs, 0)

    async def test_natural_preflight_reaches_v2_identity_when_model_listing_blocks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, source, _ = await _keep_extension(root)
            workspace = root / "natural-workspace"
            workspace.mkdir()
            for index in range(501):
                (workspace / f"file-{index:03d}.txt").write_text(
                    "bounded fixture",
                    encoding="utf-8",
                )
            output = root / "natural-preflight.json"
            report = await evaluate_workflow_patch_natural_preflight(
                parent,
                workspace,
                source_root=source,
                output_path=output,
            )
            output_exists = output.is_file()

        self.assertEqual(
            report.outcome,
            "NATURAL_WORKLOAD_PREFLIGHT_BLOCKED_BY_PRIOR_CONTEXT",
        )
        self.assertFalse(report.ready_for_live_observation)
        self.assertEqual(report.workspace_manifest_status, "BLOCKED")
        self.assertIn("entry limit", report.workspace_manifest_error or "")
        self.assertEqual(report.workspace_identity_status, "READY")
        self.assertIsNone(report.workspace_identity_failure_code)
        self.assertTrue(report.workspace_context_fingerprint.startswith("wctx2-"))
        self.assertEqual(report.selected_prior_ids, ())
        self.assertEqual(report.external_model_calls, 0)
        self.assertFalse(report.quota_consumed)
        self.assertTrue(output_exists)

    async def test_natural_preflight_binds_to_separate_provider_free_lineage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, source, _ = await _keep_extension(root)
            workspace = root / "natural-workspace"
            workspace.mkdir()
            for index in range(501):
                (workspace / f"file-{index:03d}.txt").write_text(
                    "bounded fixture",
                    encoding="utf-8",
                )
            preflight_path = root / "natural-preflight.json"
            report = await evaluate_workflow_patch_natural_preflight(
                parent,
                workspace,
                source_root=source,
                output_path=preflight_path,
            )
            company_db = parent / "isolated-company-extension.db"
            company_before = company_db.read_bytes()
            binding_path = root / "binding.json"
            binding = create_workflow_patch_exact_context_binding(
                preflight_path,
                output_path=binding_path,
            )
            preparation_path = root / "preparation.json"
            preparation = prepare_workflow_patch_exact_context_evaluation(
                parent,
                binding_path,
                source_root=source,
                output_path=preparation_path,
            )
            company_after = company_db.read_bytes()
            binding_exists = binding_path.is_file()
            preparation_exists = preparation_path.is_file()

        self.assertEqual(
            binding.production_context_fingerprint,
            report.workspace_context_fingerprint,
        )
        self.assertEqual(
            preparation.production_context_fingerprint,
            binding.production_context_fingerprint,
        )
        self.assertNotEqual(
            preparation.bound_pattern_id,
            preparation.parent_pattern_id,
        )
        self.assertFalse(preparation.eligible_for_apply)
        self.assertEqual(preparation.external_model_calls, 0)
        self.assertEqual(company_after, company_before)
        self.assertTrue(binding_exists)
        self.assertTrue(preparation_exists)


class WorkflowPatchEfficiencyCliTests(unittest.TestCase):
    def test_help_exposes_source_frozen_pair_commands(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(
                ["eval", "workflow-patch-efficiency", "--help"]
            )

        self.assertEqual(raised.exception.code, EXIT_OK)
        self.assertIn("prepare", output.getvalue())
        self.assertIn("run-next", output.getvalue())
        self.assertIn("compare", output.getvalue())
        self.assertIn("natural-preflight", output.getvalue())
        self.assertIn("bind-context", output.getvalue())
        self.assertIn("prepare-bound", output.getvalue())


if __name__ == "__main__":
    unittest.main()
