from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dynamic_firm.evolution import EvolutionNetworkService, EvolutionStore
from dynamic_firm.evolution.service import validate_capsule


def _artifact(kind: str) -> dict[str, object]:
    contents: dict[str, dict[str, object]] = {
        "SKILL_PACKAGE": {
            "skill_key": "repository_analysis",
            "applies_to": ["repository_analysis"],
            "steps": ["Inspect workspace shape before choosing a workflow"],
            "required_capabilities": [],
        },
        "WORKFLOW_PLAYBOOK": {
            "workflow_shape": ["solo"],
            "reviewer_policy": "final_review",
            "required_capabilities": [],
        },
        "AGENT_BLUEPRINT": {
            "role": "researcher",
            "skill_refs": ["repository_analysis"],
            "required_capabilities": [],
        },
        "TOOL_PACKAGE": {
            "tool_class": "workspace_read",
            "adapter_reference": "read_workspace_file",
            "input_fields": ["path_digest"],
            "output_fields": ["content_digest"],
            "required_capabilities": ["workspace_read"],
        },
    }
    return {
        "schema": "noruct.evolution-artifact.v1",
        "artifact_id": f"fixture_{kind.lower()}",
        "version": "1.0.0",
        "kind": kind,
        "release_channel": "STABLE",
        "compatibility": {
            "runtime_contract": "noruct_v1",
            "required_capabilities": [],
        },
        "content": contents[kind],
        "passport": {
            "schema": "noruct.workforce-passport.v1",
            "benchmark": {
                "suite_id": "public_fixture",
                "version": "1.0.0",
                "digest": "b" * 64,
            },
            "metrics": {
                "quality_score": 0.9,
                "safety_score": 1.0,
                "cost_bucket": "LOW",
                "latency_bucket": "LOW",
            },
            "limitations": [],
        },
    }


def _capsule(proposal_kind: str, artifact_kind: str) -> dict[str, object]:
    return {
        "schema": "noruct.learning-capsule.v2",
        "capability": "repository_analysis",
        "task_schema": {
            "domain": "software",
            "operation": "analyze",
            "input_fields": ["repository_shape"],
            "risk_level": "LOW",
        },
        "execution_summary": {
            "workflow_shape": ["solo"],
            "tool_classes": ["workspace_read"],
            "decision_count": 1,
            "redaction_applied": True,
        },
        "outcome": {
            "status": "SUCCEEDED",
            "quality_score": 0.9,
            "cost_bucket": "LOW",
            "evaluator_kind": "OFFLINE_FIXTURE",
            "metric_names": ["acceptance_passed"],
        },
        "authority": "organization_owner",
        "proposal": {
            "schema": "noruct.evolution-proposal.v1",
            "kind": proposal_kind,
            "artifact": _artifact(artifact_kind),
        },
    }


class TypedArtifactProposalTests(unittest.TestCase):
    def test_skill_workflow_roster_and_tool_proposals_are_strict_unions(self) -> None:
        pairs = {
            "SKILL_PATCH": "SKILL_PACKAGE",
            "WORKFLOW_PATCH": "WORKFLOW_PLAYBOOK",
            "ROSTER_PATCH": "AGENT_BLUEPRINT",
            "TOOL_PATCH": "TOOL_PACKAGE",
        }
        for proposal_kind, artifact_kind in pairs.items():
            with self.subTest(proposal_kind=proposal_kind):
                parsed = validate_capsule(_capsule(proposal_kind, artifact_kind))
                self.assertEqual(parsed["proposal"]["kind"], proposal_kind)
                self.assertEqual(
                    parsed["proposal"]["artifact"]["kind"], artifact_kind
                )

    def test_proposal_kind_cannot_smuggle_a_different_artifact_kind(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires an SKILL_PACKAGE"):
            validate_capsule(_capsule("SKILL_PATCH", "TOOL_PACKAGE"))

    def test_shared_artifact_consent_is_explicit_and_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with EvolutionStore(Path(directory) / "evolution.db") as store:
                consent = EvolutionNetworkService(store).grant_consent(
                    purpose="SHARED_EVOLUTION_IMPROVEMENT",
                    allowed_reuse="EVALUATE_AND_PROMOTE_VERSIONED_ARTIFACT",
                    retention_days=30,
                    authority="ORGANIZATION_OWNER",
                )
                self.assertEqual(consent["status"], "ACTIVE")
                self.assertEqual(store.status()["network_transport"], "DISABLED")


if __name__ == "__main__":
    unittest.main()
