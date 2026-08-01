import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from dynamic_firm.product.verification_graph import (
    VERIFICATION_GRAPH_SCHEMA,
    VerificationGraphError,
    project_verification_graph,
)


def _base_receipts():
    return {
        "generator_receipts": [
            {
                "id": "gen-1",
                "source_id": "artifact-1",
                "source_digest": "digest-artifact-1",
                "actor_id": "actor-a",
                "profile_digest": "profile-a",
                "drill_down_id": "dd-gen-1",
            }
        ],
        "verifier_receipts": [
            {
                "id": "ver-1",
                "generator_id": "gen-1",
                "evidence_source_id": "evidence-1",
                "evidence_source_digest": "digest-evidence-1",
                "actor_id": "actor-b",
                "profile_digest": "profile-b",
                "drill_down_id": "dd-ver-1",
            }
        ],
        "evidence_source_receipts": [
            {
                "id": "evidence-1",
                "source_id": "evidence-1",
                "source_digest": "digest-evidence-1",
                "drill_down_id": "dd-evidence-1",
            }
        ],
        "final_writer_receipts": [
            {
                "id": "writer-1",
                "source_id": "final-1",
                "source_digest": "digest-final-1",
                "verifier_id": "ver-1",
                "drill_down_id": "dd-writer-1",
            }
        ],
    }


class VerificationGraphTests(unittest.TestCase):
    def test_projection_is_typed_immutable_and_drillable(self):
        graph = project_verification_graph(**_base_receipts())

        self.assertEqual(graph.schema, VERIFICATION_GRAPH_SCHEMA)
        self.assertEqual(
            {node.kind for node in graph.nodes},
            {"GENERATOR", "VERIFIER", "EVIDENCE_SOURCE", "FINAL_WRITER"},
        )
        self.assertTrue(
            any(edge.kind == "USES_EVIDENCE" for edge in graph.edges)
        )
        self.assertEqual(graph.drill_down("dd-ver-1")[0].receipt_id, "ver-1")
        with self.assertRaises(AttributeError):
            graph.nodes += ()

    def test_same_actor_review_is_not_independent(self):
        receipts = _base_receipts()
        receipts["verifier_receipts"][0]["actor_id"] = "actor-a"

        graph = project_verification_graph(**receipts)

        self.assertEqual(graph.independence[0].status, "NOT_INDEPENDENT")

    def test_profile_only_difference_is_not_independent(self):
        receipts = _base_receipts()
        receipts["verifier_receipts"][0]["evidence_source_id"] = "artifact-1"
        receipts["verifier_receipts"][0]["evidence_source_digest"] = "digest-artifact-1"
        receipts["evidence_source_receipts"][0]["id"] = "artifact-1"
        receipts["evidence_source_receipts"][0]["source_id"] = "artifact-1"
        receipts["evidence_source_receipts"][0]["source_digest"] = "digest-artifact-1"

        graph = project_verification_graph(**receipts)

        self.assertEqual(graph.independence[0].status, "NOT_INDEPENDENT")

    def test_deterministic_validator_requires_separate_exact_evidence(self):
        receipts = _base_receipts()
        receipts["validator_receipts"] = [
            {
                "id": "val-1",
                "verifier_id": "ver-1",
                "evidence_source_id": "validator-evidence",
                "evidence_source_digest": "digest-validator-evidence",
                "deterministic": True,
                "source_id": "validator-evidence",
                "source_digest": "digest-validator-evidence",
                "drill_down_id": "dd-val-1",
            }
        ]
        receipts["evidence_source_receipts"].append(
            {
                "id": "validator-evidence",
                "source_id": "validator-evidence",
                "source_digest": "digest-validator-evidence",
                "drill_down_id": "dd-validator-evidence",
            }
        )

        graph = project_verification_graph(**receipts)

        self.assertEqual(
            next(item for item in graph.independence if item.subject_id == "validator:val-1").status,
            "INDEPENDENT",
        )

    def test_dangling_and_cycles_fail_closed(self):
        receipts = _base_receipts()
        receipts["verifier_receipts"][0]["generator_id"] = "missing"
        with self.assertRaises(VerificationGraphError):
            project_verification_graph(**receipts)

        receipts = _base_receipts()
        receipts["final_writer_receipts"][0]["verifier_id"] = "ver-1"
        receipts["generator_receipts"][0]["verifier_id"] = "ver-1"
        receipts["validator_receipts"] = [
            {
                "id": "val-1",
                "verifier_id": "ver-1",
                "evidence_source_id": "evidence-1",
                "evidence_source_digest": "digest-evidence-1",
                "source_id": "evidence-1",
                "source_digest": "digest-evidence-1",
            }
        ]
        with self.assertRaises(VerificationGraphError):
            project_verification_graph(**receipts)


if __name__ == "__main__":
    unittest.main()
