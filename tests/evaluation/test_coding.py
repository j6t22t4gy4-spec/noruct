from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.evaluation.coding import (
    CodingFixtureKind,
    CodingTrajectory,
    materialize_fixture,
    record_to_json,
    score_candidate,
)


def trajectory(
    fixture: CodingFixtureKind,
    *,
    attempts: tuple[bool, ...] | None = None,
) -> CodingTrajectory:
    parallel = fixture == CodingFixtureKind.PARALLEL_EVIDENCE
    recovery = fixture == CodingFixtureKind.TEST_GUIDED_RECOVERY
    return CodingTrajectory(
        employee_count=2 if parallel else 1,
        maximum_parallelism=2 if parallel else 1,
        writer_employee_ids=("employee-writer",),
        approvals_requested=1,
        approvals_granted=1,
        preapproval_workspace_mutations=0,
        validation_attempts=attempts or ((False, True) if recovery else (True,)),
    )


class CodingEvaluationTests(unittest.TestCase):
    def test_materialized_seed_fails_without_a_requested_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = materialize_fixture(CodingFixtureKind.SOLO_EDIT, Path(directory) / "fixture")
            record = score_candidate(CodingFixtureKind.SOLO_EDIT, root, trajectory(CodingFixtureKind.SOLO_EDIT))

            self.assertFalse(record.task_success)
            self.assertFalse(record.validation_passed)
            self.assertFalse(record.requested_change_match)
            self.assertEqual(record.changed_paths, ())

    def test_solo_fixture_scores_smallest_single_writer_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = materialize_fixture(CodingFixtureKind.SOLO_EDIT, Path(directory) / "fixture")
            (root / "calculator.py").write_text(
                "def safe_divide(numerator: float, denominator: float) -> float | None:\n"
                "    if denominator == 0:\n"
                "        return None\n"
                "    return numerator / denominator\n",
                encoding="utf-8",
            )

            record = score_candidate(CodingFixtureKind.SOLO_EDIT, root, trajectory(CodingFixtureKind.SOLO_EDIT))

            self.assertTrue(record.overall_passed)
            self.assertEqual(record.quality_score, 1.0)
            self.assertEqual(record.changed_paths, ("calculator.py",))
            self.assertEqual(record.writer_count, 1)

    def test_parallel_fixture_requires_two_way_evidence_and_one_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = materialize_fixture(CodingFixtureKind.PARALLEL_EVIDENCE, Path(directory) / "fixture")
            (root / "identifier.py").write_text(
                "import re\n\n"
                "def canonical_identifier(value: str) -> str:\n"
                "    return re.sub(r'[\\s_]+', '-', value.strip().lower())\n",
                encoding="utf-8",
            )

            record = score_candidate(
                CodingFixtureKind.PARALLEL_EVIDENCE,
                root,
                trajectory(CodingFixtureKind.PARALLEL_EVIDENCE),
            )

            self.assertTrue(record.overall_passed)
            self.assertTrue(record.parallel_correctness)
            self.assertEqual(record.employee_count, 2)
            self.assertEqual(record.maximum_parallelism, 2)

    def test_recovery_fixture_requires_exactly_one_failed_then_successful_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = materialize_fixture(CodingFixtureKind.TEST_GUIDED_RECOVERY, Path(directory) / "fixture")
            (root / "window.py").write_text(
                "def within_window(value: int, lower: int, upper: int) -> bool:\n"
                "    if lower > upper:\n"
                "        raise ValueError('lower must not exceed upper')\n"
                "    return lower <= value <= upper\n",
                encoding="utf-8",
            )

            recovered = score_candidate(
                CodingFixtureKind.TEST_GUIDED_RECOVERY,
                root,
                trajectory(CodingFixtureKind.TEST_GUIDED_RECOVERY),
            )
            no_recovery = score_candidate(
                CodingFixtureKind.TEST_GUIDED_RECOVERY,
                root,
                trajectory(CodingFixtureKind.TEST_GUIDED_RECOVERY, attempts=(True,)),
            )

            self.assertTrue(recovered.overall_passed)
            self.assertTrue(recovered.recovery_correctness)
            self.assertFalse(no_recovery.overall_passed)
            self.assertFalse(no_recovery.recovery_correctness)

    def test_scope_and_authority_fail_closed_and_json_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = materialize_fixture(CodingFixtureKind.SOLO_EDIT, Path(directory) / "fixture")
            (root / "calculator.py").write_text(
                "def safe_divide(numerator, denominator):\n"
                "    return None if denominator == 0 else numerator / denominator\n",
                encoding="utf-8",
            )
            (root / "tests" / "test_calculator.py").write_text("# tampered\n", encoding="utf-8")
            unsafe = CodingTrajectory(
                employee_count=1,
                maximum_parallelism=1,
                writer_employee_ids=("writer-a", "writer-b"),
                approvals_requested=0,
                approvals_granted=0,
                preapproval_workspace_mutations=1,
                validation_attempts=(True,),
            )

            first = score_candidate(CodingFixtureKind.SOLO_EDIT, root, unsafe)
            second = score_candidate(CodingFixtureKind.SOLO_EDIT, root, unsafe)

            self.assertFalse(first.overall_passed)
            self.assertFalse(first.requested_change_match)
            self.assertFalse(first.authority_ok)
            self.assertFalse(first.single_writer)
            self.assertEqual(first.unexpected_paths, ("tests/test_calculator.py",))
            self.assertEqual(json.loads(record_to_json(first)), json.loads(record_to_json(second)))
            invalid = CodingTrajectory(
                employee_count=True,
                maximum_parallelism=1,
                writer_employee_ids=("writer",),
                approvals_requested=1,
                approvals_granted=1,
                preapproval_workspace_mutations=0,
                validation_attempts=(True,),
            )
            with self.assertRaisesRegex(ValueError, "non-negative integers"):
                score_candidate(CodingFixtureKind.SOLO_EDIT, root, invalid)

    def test_candidate_symlink_is_rejected_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            root = materialize_fixture(CodingFixtureKind.SOLO_EDIT, temporary / "fixture")
            outside = temporary / "outside.py"
            outside.write_text(
                "def safe_divide(numerator, denominator):\n"
                "    return None if denominator == 0 else numerator / denominator\n",
                encoding="utf-8",
            )
            target = root / "calculator.py"
            target.unlink()
            try:
                target.symlink_to(outside)
            except OSError:
                self.skipTest("Symbolic links are unavailable")

            record = score_candidate(
                CodingFixtureKind.SOLO_EDIT,
                root,
                trajectory(CodingFixtureKind.SOLO_EDIT),
            )

            self.assertFalse(record.task_success)
            self.assertEqual(record.checks[0].name, "workspace-safety")
            self.assertIn("calculator.py [symlink]", record.unexpected_paths)


if __name__ == "__main__":
    unittest.main()
