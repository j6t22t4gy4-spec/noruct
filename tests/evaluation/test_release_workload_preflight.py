import unittest
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.evaluation.release_workload_preflight import (
    EXCLUSION_ALWAYS_WIN_PARALLEL,
    EXCLUSION_CONTENT_NOT_FREE,
    EXCLUSION_CUSTOMER_SECRET_POSTURE_NOT_DECLARED,
    EXCLUSION_MANAGER_PROMPT_SPECIFIC,
    H4_WORKLOAD_APPROVAL_DEFERRED,
    LOCAL_PREPARATION_COMPLETE,
    CandidateWorkloadCard,
    ReleaseWorkloadPreflightError,
    preflight_workload_cards,
    workload_manifest_hash,
)


def card(workload_id: str = "w-1", **overrides: object) -> CandidateWorkloadCard:
    values: dict[str, object] = {
        "workload_id": workload_id,
        "fixture_revision": "fixture-r1",
        "acceptance_identity": "acceptance-a1",
        "evaluator_identity": "evaluator-e1",
        "privacy_identity": "privacy-p1",
        "cost_identity": "cost-c1",
    }
    values.update(overrides)
    return CandidateWorkloadCard(**values)


class ReleaseWorkloadPreflightTests(unittest.TestCase):
    def test_required_identities_are_non_empty(self) -> None:
        for field in (
            "fixture_revision",
            "acceptance_identity",
            "evaluator_identity",
            "privacy_identity",
            "cost_identity",
        ):
            with self.subTest(field=field):
                values = {field: ""}
                with self.assertRaises(ReleaseWorkloadPreflightError):
                    card(**values)

    def test_excludes_fixed_fixture_classes(self) -> None:
        result = preflight_workload_cards(
            (
                card("parallel", always_win_parallel=True),
                card("manager", manager_prompt_specific=True),
                card("both", always_win_parallel=True, manager_prompt_specific=True),
                card("usable"),
            )
        )

        assessments = {
            item.workload_id: item for item in result.assessments
        }
        self.assertFalse(assessments["parallel"].preflight_passed)
        self.assertEqual(
            assessments["parallel"].exclusion_reasons,
            (EXCLUSION_ALWAYS_WIN_PARALLEL,),
        )
        self.assertEqual(
            assessments["manager"].exclusion_reasons,
            (EXCLUSION_MANAGER_PROMPT_SPECIFIC,),
        )
        self.assertEqual(
            assessments["both"].exclusion_reasons,
            (EXCLUSION_ALWAYS_WIN_PARALLEL, EXCLUSION_MANAGER_PROMPT_SPECIFIC),
        )
        self.assertTrue(assessments["usable"].preflight_passed)

    def test_declares_content_free_no_customer_secret_posture(self) -> None:
        result = preflight_workload_cards(
            (
                card("content", content_free=False),
                card("secret", customer_secret_free=False),
            )
        )

        self.assertTrue(result.read_only)
        self.assertTrue(result.content_free)
        self.assertTrue(result.customer_secret_free)
        self.assertEqual(
            result.assessments[0].exclusion_reasons,
            (EXCLUSION_CONTENT_NOT_FREE,),
        )
        self.assertEqual(
            result.assessments[1].exclusion_reasons,
            (EXCLUSION_CUSTOMER_SECRET_POSTURE_NOT_DECLARED,),
        )

    def test_preparation_and_approval_statuses_are_distinct(self) -> None:
        result = preflight_workload_cards((card(),))

        self.assertEqual(result.local_status, LOCAL_PREPARATION_COMPLETE)
        self.assertEqual(result.approval_status, H4_WORKLOAD_APPROVAL_DEFERRED)
        self.assertNotIn("approved", result.manifest_payload())

    def test_manifest_hash_is_stable_and_revision_sensitive(self) -> None:
        first = preflight_workload_cards((card(),))
        second = preflight_workload_cards((card(),))
        changed = preflight_workload_cards((card(fixture_revision="fixture-r2"),))

        self.assertEqual(first.manifest_hash, second.manifest_hash)
        self.assertEqual(first.manifest_hash, workload_manifest_hash((card(),)))
        self.assertNotEqual(first.manifest_hash, changed.manifest_hash)
        with self.assertRaises((AttributeError, TypeError)):
            first.candidate_cards[0].fixture_revision = "mutated"  # type: ignore[misc]

    def test_duplicate_workload_ids_fail_closed(self) -> None:
        with self.assertRaises(ReleaseWorkloadPreflightError):
            preflight_workload_cards((card("same"), card("same")))


if __name__ == "__main__":
    unittest.main()
