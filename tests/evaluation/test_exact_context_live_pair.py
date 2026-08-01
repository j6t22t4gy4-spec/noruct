from __future__ import annotations

import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, build_parser
from dynamic_firm.company import (
    CompanyLearningService,
    CompanyStateStore,
    RetentionReviewMode,
    WorkflowPatchPromotionService,
    WorkflowPatchStatus,
)
from dynamic_firm.evaluation.alpha_readiness import (
    AlphaReadinessCheck,
    AlphaReadinessEvaluation,
)
from dynamic_firm.evaluation.exact_context_live_pair import (
    ExactContextLivePairState,
    ExactContextRegressionProbe,
    compare_exact_context_live_pair,
    exact_context_live_pair_status,
    load_exact_context_workflow_patch_promotion_source,
    prepare_exact_context_live_pair,
    run_next_exact_context_live_pair_slot,
)
from dynamic_firm.evaluation.workflow_patch_efficiency import (
    create_workflow_patch_exact_context_binding,
    evaluate_workflow_patch_natural_preflight,
    prepare_workflow_patch_exact_context_evaluation,
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
from dynamic_firm.runtime.ports import CancellationToken, ModelProviderError

from tests.evaluation.test_workflow_patch_efficiency import _keep_extension


class _NaturalProvider:
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
        if '"task_id":"independent_review"' in prompt:
            completion = CompletionEnvelope(
                summary="review_basis: source-frozen-gates-consistent\n"
            )
        elif (
            '"task_id":"specialist_release_policy_review"' in prompt
            or '"task_id":"policy_evidence"' in prompt
        ):
            completion = CompletionEnvelope(
                summary=(
                    "disposition: HOLD\n"
                    "release_basis: alpha-readiness-9-of-12\n"
                    "blockers: operator-release-approval,alpha-version-staged,clean-release-worktree\n"
                    "staging: NOT_READY\n"
                )
            )
        elif '"task_id":"integrate_decision"' in prompt:
            completion = CompletionEnvelope(
                summary=_final("source-frozen-gates-consistent")
            )
        elif '"task_id":"integrate_goal"' in prompt:
            completion = CompletionEnvelope(summary=_final("unavailable"))
        else:
            completion = CompletionEnvelope(
                summary=(
                    "disposition: HOLD\n"
                    "engineering_basis: python311-full-suite-green\n"
                    "release_basis: unavailable\n"
                    "review_basis: unavailable\n"
                    "blockers: operator-release-approval,alpha-version-staged,clean-release-worktree\n"
                    "staging: NOT_READY\n"
                ),
                signals=(
                    RunSignal(
                        SignalCode.CAPABILITY_MISSING,
                        "release_policy_review",
                        ("commercial release evidence needs a specialist",),
                    ),
                ),
            )
        return ModelResponse(
            completion=completion,
            usage=Usage(input_tokens=100, output_tokens=20),
            provider_request_id=f"natural-provider-{self.calls}",
        )


def _final(review: str) -> str:
    return (
        "disposition: HOLD\n"
        "engineering_basis: python311-full-suite-green\n"
        "release_basis: alpha-readiness-9-of-12\n"
        f"review_basis: {review}\n"
        "blockers: operator-release-approval,alpha-version-staged,clean-release-worktree\n"
        "staging: NOT_READY\n"
    )


def _regression(source: str | Path) -> ExactContextRegressionProbe:
    return ExactContextRegressionProbe(
        python_version="Python 3.11.15",
        passed=True,
        test_count=452,
        skipped_count=2,
        return_code=0,
        output_sha256="a" * 64,
    )


async def _alpha(source: str | Path) -> AlphaReadinessEvaluation:
    blockers = (
        "operator-release-approval",
        "alpha-version-staged",
        "clean-release-worktree",
    )
    checks = tuple(
        AlphaReadinessCheck(
            name=(f"code-gate-{index}" if index < 9 else blockers[index - 9]),
            category="test",
            passed=index < 9,
            evidence="bounded fixture evidence",
            operator_required=index >= 9,
        )
        for index in range(12)
    )
    return AlphaReadinessEvaluation(
        schema_version="noruct.alpha-readiness.v1",
        ready=False,
        target_version="0.1.0a1",
        current_version="0.0.test",
        classification="BLOCKED_OPERATOR_AND_RELEASE_ENGINEERING",
        external_model_calls=0,
        quota_consumed=False,
        checks=checks,
        blocking_checks=blockers,
        next_actions=("complete operator gates",),
    )


async def _seed(root: Path):
    parent, source, wheel = await _keep_extension(root)
    workspace = root / "workspace"
    workspace.mkdir()
    for index in range(501):
        (workspace / f"file-{index:03d}.txt").write_text(
            "bounded",
            encoding="utf-8",
        )
    preflight_path = root / "natural-preflight.json"
    await evaluate_workflow_patch_natural_preflight(
        parent,
        workspace,
        source_root=source,
        output_path=preflight_path,
    )
    binding_path = root / "binding.json"
    create_workflow_patch_exact_context_binding(
        preflight_path,
        output_path=binding_path,
    )
    preparation_path = root / "preparation.json"
    prepare_workflow_patch_exact_context_evaluation(
        parent,
        binding_path,
        source_root=source,
        output_path=preparation_path,
    )
    return parent, source, wheel, binding_path, preparation_path


class ExactContextLivePairTests(unittest.IsolatedAsyncioTestCase):
    async def test_noruct_employee_runtime_port_preserves_exact_completion_validation(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("selected test Python lacks the required employee runtime dependency")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, source, wheel, binding, preparation = await _seed(root)
            pair = root / "noruct-live-pair"
            prepared = await prepare_exact_context_live_pair(
                parent,
                pair,
                binding_path=binding,
                preparation_path=preparation,
                wheel=wheel,
                source_root=source,
                model="fixture-cheap-model",
                command="fixture-codex",
                employee_runtime="noruct",
                runtime_python=sys.executable,
                login_status_factory=lambda command: CodexLoginStatus(
                    executable="/fixture/codex",
                    installed=True,
                    authenticated=True,
                ),
                capability_probe=lambda command: ("/fixture/codex", True, "supported"),
                regression_probe=_regression,
                alpha_factory=_alpha,
            )
            provider = lambda config: _NaturalProvider()
            control = await run_next_exact_context_live_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=provider,
            )
            candidate = await run_next_exact_context_live_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=provider,
            )
            report = compare_exact_context_live_pair(pair)

        self.assertTrue(prepared.preflight.ready)
        self.assertTrue(control.task_success)
        self.assertTrue(candidate.task_success)
        self.assertTrue(report.pair_gate_passed)

    async def test_provider_free_prepare_then_exact_live_pair_compares(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, source, wheel, binding, preparation = await _seed(root)
            company_path = parent / "isolated-company-extension.db"
            company_before = company_path.read_bytes()
            pair = root / "live-pair"
            prepared = await prepare_exact_context_live_pair(
                parent,
                pair,
                binding_path=binding,
                preparation_path=preparation,
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
                regression_probe=_regression,
                alpha_factory=_alpha,
            )
            with self.assertRaisesRegex(ValueError, "confirm-live-quota"):
                await run_next_exact_context_live_pair_slot(
                    pair,
                    confirm_live_quota=False,
                )
            provider = lambda config: _NaturalProvider()
            control = await run_next_exact_context_live_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=provider,
            )
            candidate = await run_next_exact_context_live_pair_slot(
                pair,
                confirm_live_quota=True,
                provider_factory=provider,
            )
            comparison_path = pair / "comparison-v1.json"
            report = compare_exact_context_live_pair(
                pair,
                output_path=comparison_path,
            )
            final_status = exact_context_live_pair_status(pair)
            with CompanyStateStore(company_path) as immutable_parent:
                with self.assertRaisesRegex(
                    ValueError,
                    "IMMUTABLE_PARENT_TARGET",
                ):
                    WorkflowPatchPromotionService(immutable_parent).preview(pair)
            promotion_state = root / "promotion-company.db"
            shutil.copy2(company_path, promotion_state)
            tamper_state = root / "promotion-company-tamper.db"
            shutil.copy2(company_path, tamper_state)
            # Phase 60 source is newer than the frozen pair. Historical wheel/source
            # integrity and current runtime compatibility are intentionally separate.
            (source / "src" / "runtime.py").write_text(
                "VALUE = 'phase-60-control-plane-only'\n",
                encoding="utf-8",
            )
            promotion_evidence, promotion_parent = (
                load_exact_context_workflow_patch_promotion_source(pair)
            )
            with CompanyStateStore(promotion_state) as company:
                promotion = WorkflowPatchPromotionService(company)
                preview = promotion.preview(pair)
                first = promotion.promote(pair, actor="user:test")
                second = promotion.promote(pair, actor="user:test")
                promoted_events = company.list_patch_events(first.patch.patch_id)
                replay_matches = CompanyLearningService(company).replay(
                    first.patch.patch_id
                )
                promoted_summary = company.summary()
                company.set_retention_review_mode(
                    RetentionReviewMode.AUTO_REVIEW,
                    actor="user:test",
                )
                with self.assertRaisesRegex(ValueError, "COMPANY_STATE_DRIFT"):
                    promotion.preview(pair)
            with CompanyStateStore(tamper_state) as company:
                tampered = WorkflowPatchPromotionService(company).promote(
                    pair,
                    actor="user:test",
                )
            with sqlite3.connect(tamper_state) as connection:
                row = connection.execute(
                    "SELECT payload_json FROM workflow_patch_events WHERE patch_id = ?",
                    (tampered.patch.patch_id,),
                ).fetchone()
                assert row is not None
                event_payload = json.loads(row[0])
                event_payload["promotion_envelope"]["token_delta"] += 1
                connection.execute(
                    "UPDATE workflow_patch_events SET payload_json = ? WHERE patch_id = ?",
                    (
                        json.dumps(event_payload, sort_keys=True, separators=(",", ":")),
                        tampered.patch.patch_id,
                    ),
                )
            with CompanyStateStore(tamper_state) as company:
                with self.assertRaisesRegex(ValueError, "TARGET_PATTERN_CONFLICT"):
                    WorkflowPatchPromotionService(company).preview(pair)
            comparison_value = json.loads(comparison_path.read_text(encoding="utf-8"))
            comparison_value["token_delta"] += 1
            comparison_path.write_text(
                json.dumps(comparison_value),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "COMPARISON_DRIFT"):
                load_exact_context_workflow_patch_promotion_source(pair)
            company_after = company_path.read_bytes()

        self.assertTrue(prepared.preflight.ready)
        self.assertEqual(prepared.preflight.external_model_calls, 0)
        self.assertTrue(control.task_success)
        self.assertTrue(candidate.task_success)
        self.assertEqual(final_status.state, ExactContextLivePairState.COMPLETE)
        self.assertTrue(report.pair_gate_passed)
        self.assertEqual(report.control_quality, 0.8)
        self.assertEqual(report.candidate_quality, 1.0)
        self.assertEqual(report.quality_gain, 0.2)
        self.assertEqual(report.control_model_calls, 3)
        self.assertEqual(report.candidate_model_calls, 4)
        self.assertTrue(report.proposal_recommended)
        self.assertFalse(report.automatic_approval)
        self.assertFalse(report.eligible_for_apply)
        self.assertEqual(promotion_evidence.external_model_calls, 0)
        self.assertEqual(promotion_parent, company_path.resolve())
        self.assertFalse(preview.state_changed)
        self.assertFalse(preview.proposal_exists)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.patch.patch_id, second.patch.patch_id)
        self.assertEqual(first.patch.status, WorkflowPatchStatus.PROPOSED)
        self.assertEqual(len(promoted_events), 1)
        self.assertTrue(replay_matches)
        self.assertEqual(promoted_summary.playbook_revision, 2)
        self.assertEqual(promoted_summary.workflow_pattern_count, 1)
        self.assertEqual(promoted_summary.patch_counts["PROPOSED"], 1)
        self.assertEqual(company_after, company_before)

    async def test_source_drift_refuses_before_slot_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, source, wheel, binding, preparation = await _seed(root)
            pair = root / "live-pair"
            await prepare_exact_context_live_pair(
                parent,
                pair,
                binding_path=binding,
                preparation_path=preparation,
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
                regression_probe=_regression,
                alpha_factory=_alpha,
            )
            (source / "src" / "runtime.py").write_text(
                "VALUE = 'drift'\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "SOURCE_DRIFT"):
                await run_next_exact_context_live_pair_slot(
                    pair,
                    confirm_live_quota=True,
                    provider_factory=lambda config: _NaturalProvider(),
                )
            status = exact_context_live_pair_status(pair)

        self.assertEqual(status.state, ExactContextLivePairState.READY)
        self.assertEqual(status.completed_runs, 0)
        self.assertEqual(status.external_model_calls_recorded, 0)

    async def test_control_provider_failure_is_terminal_and_stops_candidate(self) -> None:
        async def failing_runner(**kwargs):
            raise ModelProviderError("provider failed", code="fixture_failure")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, source, wheel, binding, preparation = await _seed(root)
            pair = root / "live-pair"
            await prepare_exact_context_live_pair(
                parent,
                pair,
                binding_path=binding,
                preparation_path=preparation,
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
                regression_probe=_regression,
                alpha_factory=_alpha,
            )
            result = await run_next_exact_context_live_pair_slot(
                pair,
                confirm_live_quota=True,
                live_runner=failing_runner,
            )
            with self.assertRaisesRegex(ValueError, "PARTIAL_FAILED"):
                await run_next_exact_context_live_pair_slot(
                    pair,
                    confirm_live_quota=True,
                )

        self.assertFalse(result.task_success)
        self.assertEqual(result.status.state, ExactContextLivePairState.PARTIAL_FAILED)
        self.assertEqual(result.status.completed_runs, 0)


class ExactContextLivePairCliTests(unittest.TestCase):
    def test_help_exposes_four_step_control_plane(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["eval", "exact-context-live-pair", "--help"])
        self.assertEqual(raised.exception.code, EXIT_OK)
        for command in ("prepare", "status", "run-next", "compare"):
            self.assertIn(command, output.getvalue())

    def test_company_help_exposes_proposal_only_promotion_commands(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["company", "--help"])
        self.assertEqual(raised.exception.code, EXIT_OK)
        self.assertIn("workflow-promote-preview", output.getvalue())
        self.assertIn("workflow-promote", output.getvalue())


if __name__ == "__main__":
    unittest.main()
