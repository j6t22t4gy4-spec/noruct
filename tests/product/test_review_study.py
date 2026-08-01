import unittest
import importlib.util
from pathlib import Path
import sys

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "dynamic_firm"
    / "product"
    / "review_study.py"
)
_SPEC = importlib.util.spec_from_file_location("review_study", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

NO_HUMAN_STUDY_OR_OUTCOME_CLAIM = _MODULE.NO_HUMAN_STUDY_OR_OUTCOME_CLAIM
PROTOCOL_VERSION = _MODULE.PROTOCOL_VERSION
SYNTHETIC_STUDY_MARKER = _MODULE.SYNTHETIC_STUDY_MARKER
ReviewStudyObservation = _MODULE.ReviewStudyObservation
ReviewStudyProtocol = _MODULE.ReviewStudyProtocol
aggregate_observations = _MODULE.aggregate_observations
simulate_review_study = _MODULE.simulate_review_study
synthetic_review_study_fixture = _MODULE.synthetic_review_study_fixture

class ReviewStudyTests(unittest.TestCase):
    def test_protocol_and_observations_are_immutable_and_synthetic(self):
        protocol = ReviewStudyProtocol()
        self.assertEqual(protocol.version, PROTOCOL_VERSION)
        observation = synthetic_review_study_fixture()[0]
        self.assertEqual(observation.synthetic_study_marker, SYNTHETIC_STUDY_MARKER)
        with self.assertRaises((AttributeError, TypeError)):
            observation.review_time_seconds = 1  # type: ignore[misc]

    def test_marker_is_required(self):
        with self.assertRaises(ValueError):
            ReviewStudyObservation(
                phase="before",
                purpose="purpose",
                ai_scope="scope",
                review_focus="focus",
                unverified_item_correct=True,
                review_time_seconds=1,
                reopened_evidence_count=0,
                rework_count=0,
                approval_friction_count=0,
                synthetic_study_marker="HUMAN_STUDY",
            )

    def test_raw_content_fields_are_rejected(self):
        values = {
            "phase": "before",
            "purpose": "purpose",
            "ai_scope": "scope",
            "review_focus": "focus",
            "unverified_item_correct": True,
            "review_time_seconds": 1,
            "reopened_evidence_count": 0,
            "rework_count": 0,
            "approval_friction_count": 0,
            "prompt": "never store this",
        }
        with self.assertRaises(ValueError):
            ReviewStudyObservation.from_mapping(values)

    def test_before_after_aggregation_has_only_metric_deltas(self):
        result = simulate_review_study()
        self.assertEqual(result.claim_boundary, NO_HUMAN_STUDY_OR_OUTCOME_CLAIM)
        self.assertFalse(result.human_study_claim)
        self.assertFalse(result.outcome_claim)
        self.assertEqual(result.phases["before"].observation_count, 2)
        self.assertEqual(result.phases["after"].observation_count, 2)
        self.assertEqual(result.phases["before"].correct_unverified_item_rate, 0.5)
        self.assertEqual(result.phases["after"].correct_unverified_item_rate, 1.0)
        self.assertEqual(result.deltas_after_minus_before["mean_rework_count"], -1.0)
        with self.assertRaises(TypeError):
            result.phases["before"] = result.phases["after"]  # type: ignore[index]

    def test_aggregation_requires_explicit_observations(self):
        with self.assertRaises(TypeError):
            aggregate_observations([{"phase": "before"}])  # type: ignore[list-item]


if __name__ == "__main__":
    unittest.main()
