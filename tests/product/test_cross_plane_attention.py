from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evolution import EvolutionNetworkService, EvolutionStore
from dynamic_firm.knowledge.store import KnowledgeStore, knowledge_state_path
from dynamic_firm.product.cross_plane_attention import (
    SupplementalAttentionKind,
    inspect_supplemental_operator_attention,
)
from dynamic_firm.product.operator_surface import build_operator_surface_snapshot


def _artifact() -> dict[str, object]:
    return {
        "schema": "noruct.evolution-artifact.v1",
        "artifact_id": "review_skill",
        "version": "1.0.0",
        "kind": "SKILL_PACKAGE",
        "release_channel": "STABLE",
        "compatibility": {
            "runtime_contract": "noruct_v1",
            "required_capabilities": ["workspace_read"],
        },
        "content": {
            "skill_key": "review",
            "applies_to": ["repository_analysis"],
            "steps": ["Inspect the bounded local facts."],
            "required_capabilities": [],
        },
        "passport": {
            "schema": "noruct.workforce-passport.v1",
            "benchmark": {
                "suite_id": "review_suite",
                "version": "1.0.0",
                "digest": "b" * 64,
            },
            "metrics": {
                "quality_score": 0.8,
                "safety_score": 1.0,
                "cost_bucket": "LOW",
                "latency_bucket": "LOW",
            },
            "limitations": [],
        },
    }


class CrossPlaneAttentionTests(unittest.TestCase):
    def test_pending_knowledge_and_staged_artifact_are_read_only_operator_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_state = root / "runtime.db"
            secret = "CROSS-PLANE-CANDIDATE-BODY-MUST-NOT-RENDER"
            with KnowledgeStore(knowledge_state_path(runtime_state)) as knowledge:
                candidate = knowledge.create_write_candidate(
                    job_id="job-cross-plane",
                    statement=secret,
                )
            evolution_path = runtime_state.with_name("runtime.evolution.db")
            with EvolutionStore(evolution_path) as evolution:
                service = EvolutionNetworkService(evolution)
                service.register_artifact_manifest(_artifact())
                staged = service.stage_artifact("review_skill", "1.0.0")

            attention = inspect_supplemental_operator_attention(runtime_state)

            self.assertEqual(attention.knowledge_state, "READY")
            self.assertEqual(attention.evolution_state, "READY")
            self.assertEqual(attention.knowledge_pending_candidate_count, 1)
            self.assertEqual(attention.artifact_review_count, 1)
            self.assertEqual(
                {(item.kind, item.subject_id) for item in attention.items},
                {
                    (SupplementalAttentionKind.KNOWLEDGE_CANDIDATE, candidate.candidate_id),
                    (SupplementalAttentionKind.ARTIFACT_INSTALLATION, staged["installation_id"]),
                },
            )
            self.assertNotIn(secret, str(attention))
            snapshot = build_operator_surface_snapshot(
                manager_report=None,
                inspection=None,
                supplemental_attention=attention,
            )
            self.assertEqual(snapshot.attention["status"], "ACTION_REQUIRED")
            self.assertEqual(snapshot.attention["item_count"], 2)
            self.assertEqual(snapshot.attention["supplemental"]["artifact_review_count"], 1)
            self.assertNotIn(secret, str(snapshot.as_dict()))

            knowledge_before = knowledge_state_path(runtime_state).read_bytes()
            evolution_before = evolution_path.read_bytes()
            output = io.StringIO()
            errors = io.StringIO()
            self.assertEqual(
                main(
                    ["company", "attention", "--state", str(runtime_state), "--json"],
                    stdout=output,
                    stderr=errors,
                ),
                EXIT_OK,
                errors.getvalue(),
            )
            cli_attention = json.loads(output.getvalue())
            self.assertEqual(
                cli_attention["supplemental"]["knowledge_pending_candidate_count"], 1
            )
            self.assertEqual(cli_attention["supplemental"]["artifact_review_count"], 1)
            self.assertNotIn(secret, output.getvalue())
            self.assertEqual(knowledge_before, knowledge_state_path(runtime_state).read_bytes())
            self.assertEqual(evolution_before, evolution_path.read_bytes())

    def test_missing_planes_are_not_created_and_limit_discloses_partial_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_state = Path(directory) / "runtime.db"
            absent = inspect_supplemental_operator_attention(runtime_state)
            self.assertEqual(absent.knowledge_state, "NOT_CONFIGURED")
            self.assertEqual(absent.evolution_state, "NOT_CONFIGURED")
            self.assertEqual(absent.items, ())
            self.assertFalse(knowledge_state_path(runtime_state).exists())
            self.assertFalse(runtime_state.with_name("runtime.evolution.db").exists())

            with KnowledgeStore(knowledge_state_path(runtime_state)) as knowledge:
                knowledge.create_write_candidate(job_id="job-one", statement="First candidate")
                knowledge.create_write_candidate(job_id="job-two", statement="Second candidate")
            limited = inspect_supplemental_operator_attention(runtime_state, limit=1)
            self.assertEqual(limited.knowledge_pending_candidate_count, 2)
            self.assertEqual(len(limited.items), 1)
            self.assertTrue(limited.truncated)

    def test_unreadable_optional_plane_is_reported_without_repairing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_state = Path(directory) / "runtime.db"
            corrupt_knowledge = knowledge_state_path(runtime_state)
            corrupt_knowledge.write_text("not a sqlite database", encoding="utf-8")

            attention = inspect_supplemental_operator_attention(runtime_state)

            self.assertEqual(attention.knowledge_state, "UNAVAILABLE")
            self.assertEqual(attention.knowledge_pending_candidate_count, 0)
            self.assertEqual(attention.evolution_state, "NOT_CONFIGURED")
            self.assertEqual(corrupt_knowledge.read_text(encoding="utf-8"), "not a sqlite database")


if __name__ == "__main__":
    unittest.main()
