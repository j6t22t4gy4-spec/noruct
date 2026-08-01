import sys
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.models import (
    EvidenceSource,
    organization_episode_from_dict,
)
from dynamic_firm.company.workflow_models import OrganizationEpisode


class EffectiveVerificationIndependenceTest(unittest.TestCase):
    def episode(self, **outcomes: str) -> OrganizationEpisode:
        return OrganizationEpisode.create(
            job_id="job-1",
            source=EvidenceSource.OFFLINE_FIXTURE,
            task_family="task-family",
            context_fingerprint="context",
            execution_profile="profile",
            planning_mode="SOLO",
            plan_template=(),
            success=True,
            quality_score=1.0,
            baseline_quality_score=0.8,
            model_calls=1,
            baseline_model_calls=1,
            employee_count=1,
            maximum_parallelism=1,
            writer_count=1,
            approvals_requested=0,
            approvals_granted=0,
            preapproval_mutations=0,
            validation_attempts=(True,),
            ledger_digest="ledger",
            **outcomes,
        )

    def test_old_payload_decodes_to_conservative_explicit_outcomes(self) -> None:
        episode = self.episode()
        payload = asdict(episode)
        for field in (
            "context_route_difference",
            "reviewer_material_profile_difference",
            "evidence_route_difference",
            "model_route_difference",
            "tool_route_difference",
            "procedure_route_difference",
            "error_independence",
            "detected_error",
            "false_positive",
            "rework",
            "final_change_status",
        ):
            payload.pop(field)

        decoded = organization_episode_from_dict(payload)

        self.assertEqual(decoded.reviewer_material_profile_difference, "NOT_RECORDED")
        self.assertEqual(decoded.context_route_difference, "NOT_RECORDED")
        self.assertEqual(decoded.evidence_route_difference, "NOT_RECORDED")
        self.assertEqual(decoded.model_route_difference, "NOT_RECORDED")
        self.assertEqual(decoded.tool_route_difference, "NOT_RECORDED")
        self.assertEqual(decoded.procedure_route_difference, "NOT_RECORDED")
        self.assertEqual(decoded.error_independence, "NOT_INDEPENDENT")
        self.assertEqual(decoded.detected_error, "NOT_RECORDED")
        self.assertEqual(decoded.false_positive, "NOT_RECORDED")
        self.assertEqual(decoded.rework, "NOT_RECORDED")
        self.assertEqual(decoded.final_change_status, "NOT_RECORDED")

    def test_profile_difference_does_not_establish_error_independence(self) -> None:
        episode = self.episode(reviewer_material_profile_difference="DIFFERENT")

        self.assertEqual(episode.reviewer_material_profile_difference, "DIFFERENT")
        self.assertEqual(episode.error_independence, "NOT_INDEPENDENT")

    def test_explicit_context_evidence_and_separate_route_are_independent(self) -> None:
        episode = self.episode(
            context_route_difference="NON_OVERLAPPING",
            evidence_route_difference="INDEPENDENT",
            model_route_difference="DIFFERENT",
            reviewer_material_profile_difference="SAME",
            error_independence="NOT_INDEPENDENT",
        )

        self.assertEqual(episode.error_independence, "INDEPENDENT")
        self.assertEqual(
            episode.effective_verification_independence,
            "INDEPENDENT",
        )

    def test_overlapping_context_remains_correlated_despite_profile_and_routes(self) -> None:
        episode = self.episode(
            context_route_difference="OVERLAPPING",
            evidence_route_difference="INDEPENDENT",
            model_route_difference="DIFFERENT",
            reviewer_material_profile_difference="DIFFERENT",
            error_independence="INDEPENDENT",
        )

        self.assertEqual(episode.error_independence, "NOT_INDEPENDENT")
        self.assertEqual(
            episode.effective_verification_independence,
            "NOT_INDEPENDENT",
        )

    def test_recorded_outcomes_are_immutable_and_content_free(self) -> None:
        episode = self.episode(
            context_route_difference="NON_OVERLAPPING",
            reviewer_material_profile_difference="DIFFERENT",
            evidence_route_difference="DIFFERENT",
            model_route_difference="DIFFERENT",
            tool_route_difference="SAME",
            procedure_route_difference="DIFFERENT",
            error_independence="INDEPENDENT",
            detected_error="DETECTED",
            false_positive="NOT_DETECTED",
            rework="REQUIRED",
            final_change_status="CHANGED",
        )

        payload = episode.content_payload()
        self.assertEqual(payload["error_independence"], "INDEPENDENT")
        self.assertNotIn("episode_id", payload)
        self.assertNotIn("recorded_at", payload)
        with self.assertRaises(AttributeError):
            episode.error_independence = "NOT_INDEPENDENT"


if __name__ == "__main__":
    unittest.main()
