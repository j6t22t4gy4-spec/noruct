from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from dynamic_firm.company.exception_candidate_compiler import (
    CandidateControls,
    ExceptionCandidate,
    ExceptionCandidateKind,
    ExceptionCluster,
    ExceptionProvenance,
    TypedExceptionObservation,
    compile_exception_candidate,
    project_exception_cluster,
)


class ExceptionCandidateCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controls = CandidateControls(
            baseline="baseline:release-7",
            approval="approval:human-review",
            rollback="rollback:release-7",
            retirement_condition="retire:failure-rate-bound",
        )

    def _observation(self, source_id: str, code: str = "TOOL_TIMEOUT"):
        return TypedExceptionObservation(
            exception_type="ToolExecutionError",
            exception_code=code,
            provenance=ExceptionProvenance(source_id, "a" * 64),
        )

    def test_repeat_qualified_exact_cluster_compiles_content_free_candidate(self) -> None:
        cluster = project_exception_cluster(
            (self._observation("evidence:1"), self._observation("evidence:2"))
        )

        candidate = compile_exception_candidate(
            cluster,
            ExceptionCandidateKind.TOOL,
            self.controls,
        )

        self.assertIsInstance(cluster, ExceptionCluster)
        self.assertIsInstance(candidate, ExceptionCandidate)
        self.assertTrue(cluster.repeat_qualified)
        self.assertTrue(candidate.proposal_only)
        self.assertEqual(candidate.cluster_digest, cluster.cluster_digest)
        self.assertEqual(candidate.controls, self.controls)
        self.assertNotIn("message", candidate.payload())
        self.assertNotIn("raw_exception", candidate.payload())
        with self.assertRaises(FrozenInstanceError):
            cluster.exception_code = "changed"
        with self.assertRaises(FrozenInstanceError):
            candidate.kind = ExceptionCandidateKind.RULE

    def test_single_occurrence_and_mixed_typed_occurrences_are_rejected(self) -> None:
        single = project_exception_cluster((self._observation("evidence:1"),))
        self.assertFalse(single.repeat_qualified)
        with self.assertRaisesRegex(ValueError, "single occurrence"):
            compile_exception_candidate(
                single,
                ExceptionCandidateKind.TEST,
                self.controls,
            )

        with self.assertRaisesRegex(ValueError, "exact typed cluster"):
            project_exception_cluster(
                (self._observation("evidence:1"), self._observation("evidence:2", "AUTH"))
            )

    def test_authority_expanding_candidate_is_rejected_and_controls_are_required(self) -> None:
        cluster = project_exception_cluster(
            (self._observation("evidence:1"), self._observation("evidence:2"))
        )
        with self.assertRaisesRegex(ValueError, "authority-expanding"):
            compile_exception_candidate(
                cluster,
                ExceptionCandidateKind.ROSTER,
                self.controls,
                authority_expanding=True,
            )
        with self.assertRaises(ValueError):
            CandidateControls(
                baseline="baseline",
                approval="approval",
                rollback="rollback",
                retirement_condition="",
            )

        with self.assertRaises(ValueError):
            ExceptionCandidate(
                kind=ExceptionCandidateKind.RULE,
                cluster_digest=cluster.cluster_digest,
                provenance=cluster.occurrences,
                controls=self.controls,
                running_job_changed=True,
            )


if __name__ == "__main__":
    unittest.main()
