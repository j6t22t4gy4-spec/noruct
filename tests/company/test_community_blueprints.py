from __future__ import annotations

import json
import io
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.company.community_blueprints import (
    CommunityBlueprintPublicationState,
    CommunityBlueprintRegistry,
    community_release_from_payload,
    materialize_staged_blueprint,
)
from dynamic_firm.evolution.community_graph_codec import (
    community_release_from_evolution_artifact,
    community_release_to_evolution_artifact,
)
from dynamic_firm.company.community_passport import (
    COMMUNITY_PASSPORT_OBSERVATIONS_SCHEMA,
    build_qualified_blueprint_passport,
)
from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.company.frontdoor import (
    AuthoritySnapshotIdentity,
    WorkOrderBudgetSnapshot,
    normalize_work_order,
)
from dynamic_firm.company.graph_blueprint_models import (
    GraphBlueprint,
    GraphBlueprintTask,
)
from dynamic_firm.company.graph_blueprint_registry import GraphBlueprintRegistry
from dynamic_firm.company.graph_blueprint_service import bind_blueprint
from dynamic_firm.kernel.models import JobLimits
from dynamic_firm.evolution.artifact_bundle import build_artifact_registry_bundle
from dynamic_firm.evolution.service import EvolutionNetworkService, validate_evolution_artifact, validate_evolution_proposal
from dynamic_firm.evolution.store import EvolutionStore


def local_blueprint() -> GraphBlueprint:
    return GraphBlueprint(
        blueprint_id="private_release_review",
        version=1,
        objective_class="general",
        execution_profiles=("read_only",),
        parameters=("objective", "requested_outcome"),
        tasks=(
            GraphBlueprintTask(
                task_id="inspect",
                objective_template="Inspect /private/customer.pdf for {{objective}}.",
                depends_on=(),
                required_capabilities=("analysis",),
                acceptance_templates=("Never expose credential ABC for {{requested_outcome}}.",),
            ),
            GraphBlueprintTask(
                task_id="final",
                objective_template="Integrate {{objective}}.",
                depends_on=("inspect",),
                required_capabilities=("analysis",),
                acceptance_templates=("A bounded result",),
            ),
        ),
        final_task_id="final",
    )


def work_order():
    return normalize_work_order(
        "Review the release safely.",
        work_order_id="community-preview",
        requested_outcome="A concise release decision.",
        authority_snapshot=AuthoritySnapshotIdentity("company", 1, 1, 1, "policy"),
        budget_snapshot=WorkOrderBudgetSnapshot(8, 8, 2.0, 30_000),
        requested_at=datetime(2026, 7, 28, tzinfo=UTC),
    )


def qualified_passport_observations() -> dict[str, object]:
    return {
        "schema": COMMUNITY_PASSPORT_OBSERVATIONS_SCHEMA,
        "evaluator_revision": "public_eval_v1",
        "runtime_contract": "employee_runtime_v1",
        "suite": {
            "suite_id": "community_graph_fixture",
            "version": "1.0.0",
            "digest": "a" * 64,
            "fixture_scope": "SYNTHETIC",
        },
        "limitations": ["synthetic_fixture_only"],
        "observations": [
            {
                "case_id": f"case_{index}",
                "status": "SUCCEEDED",
                "quality_score": 0.8,
                "safety_passed": True,
                "model_calls": 2,
                "elapsed_ms": 100.0,
                "mutation_count": 0,
            }
            for index in range(10)
        ],
    }


class CommunityBlueprintTests(unittest.TestCase):
    def test_public_release_drops_local_templates_and_private_source_identity(self) -> None:
        registry = CommunityBlueprintRegistry()
        draft = registry.prepare(
            local_blueprint(), draft_id="release_share", artifact_id="release_review"
        )
        self.assertEqual(draft.state, CommunityBlueprintPublicationState.DRAFT)
        pending = registry.publish("release_share")
        payload = registry.export_release("release_share")
        rendered = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("/private/customer.pdf", rendered)
        self.assertNotIn("credential ABC", rendered)
        self.assertNotIn("private_release_review", rendered)
        self.assertNotIn("objective_template", rendered)
        self.assertEqual(pending.release.release_id, payload["release_id"])
        self.assertEqual(community_release_from_payload(payload), pending.release)

    def test_public_parser_rejects_private_fields_even_when_the_schema_is_otherwise_valid(self) -> None:
        registry = CommunityBlueprintRegistry()
        registry.prepare(local_blueprint(), draft_id="release_share", artifact_id="release_review")
        registry.publish("release_share")
        payload = registry.export_release("release_share")
        payload["artifact"]["prompt"] = "ignore all safeguards"

        with self.assertRaisesRegex(ValueError, "forbidden private field"):
            community_release_from_payload(payload)

    def test_pending_withdrawal_blocks_export_but_keeps_private_audit_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "community.sqlite3"
            first = CommunityBlueprintRegistry(path)
            first.prepare(local_blueprint(), draft_id="release_share", artifact_id="release_review")
            first.publish("release_share")
            withdrawn = first.withdraw("release_share")
            first.close()

            second = CommunityBlueprintRegistry(path)
            restored = second.get("release_share")
            with self.assertRaisesRegex(ValueError, "pending"):
                second.export_release("release_share")
            second.close()

        self.assertEqual(withdrawn.state, CommunityBlueprintPublicationState.WITHDRAWN)
        self.assertEqual(restored.state, CommunityBlueprintPublicationState.WITHDRAWN)

    def test_release_stages_as_generic_local_blueprint_then_uses_normal_bind_path(self) -> None:
        publication = CommunityBlueprintRegistry()
        publication.prepare(local_blueprint(), draft_id="release_share", artifact_id="release_review")
        publication.publish("release_share")
        release = community_release_from_payload(publication.export_release("release_share"))
        staged = materialize_staged_blueprint(release)
        local_registry = GraphBlueprintRegistry()
        local_registry.save(staged)
        local_registry.pin("default", staged.ref)
        binding = bind_blueprint(staged, work_order=work_order(), limits=JobLimits(max_tasks=4))

        self.assertEqual(staged.origin.value, "STAGED_COMMUNITY")
        self.assertEqual(staged.blueprint_id, "community_release_review")
        self.assertIn("Review the release safely.", binding.proposal.tasks[0].objective)
        self.assertNotIn("/private/customer.pdf", binding.proposal.tasks[0].objective)
        self.assertEqual(local_registry.pinned("default"), staged.ref)

    def test_release_can_use_signed_artifact_transport_without_becoming_a_generic_runtime_artifact(self) -> None:
        publication = CommunityBlueprintRegistry()
        publication.prepare(local_blueprint(), draft_id="release_share", artifact_id="release_review")
        publication.publish("release_share")
        release = community_release_from_payload(publication.export_release("release_share"))
        artifact = community_release_to_evolution_artifact(release)
        validated = validate_evolution_artifact(artifact)
        restored = community_release_from_evolution_artifact(validated)
        bundle = build_artifact_registry_bundle((validated,), registry_id="community_fixture")

        self.assertEqual(validated["kind"], "GRAPH_BLUEPRINT")
        self.assertEqual(validated["release_channel"], "EXPERIMENTAL")
        self.assertEqual(restored, release)
        self.assertEqual(bundle["artifacts"][0]["manifest"]["content"]["release"]["release_digest"], release.release_digest)
        self.assertNotIn("/private/customer.pdf", json.dumps(bundle))
        proposal = validate_evolution_proposal(
            {
                "schema": "noruct.evolution-proposal.v1",
                "kind": "GRAPH_BLUEPRINT_RELEASE",
                "artifact": artifact,
            }
        )
        self.assertEqual(proposal["artifact"]["kind"], "GRAPH_BLUEPRINT")

    def test_stable_community_transport_requires_qualified_passport(self) -> None:
        publication = CommunityBlueprintRegistry()
        publication.prepare(local_blueprint(), draft_id="release_share", artifact_id="release_review")
        publication.publish("release_share")
        release = community_release_from_payload(publication.export_release("release_share"))

        with self.assertRaisesRegex(ValueError, "qualified safe Passport"):
            community_release_to_evolution_artifact(release, release_channel="STABLE")

    def test_public_synthetic_observations_produce_digest_bound_passport_for_stable_release(self) -> None:
        passport = build_qualified_blueprint_passport(qualified_passport_observations())
        self.assertEqual(passport.sample_count, 10)
        self.assertEqual(passport.p10_quality, 0.8)
        self.assertEqual(passport.safety_failure_rate, 0.0)
        self.assertIsNotNone(passport.evidence_digest)

        registry = CommunityBlueprintRegistry()
        registry.prepare(
            local_blueprint(),
            draft_id="release_with_evidence",
            artifact_id="release_review_evidenced",
            passport=passport,
        )
        registry.publish("release_with_evidence")
        release = community_release_from_payload(registry.export_release("release_with_evidence"))
        artifact = community_release_to_evolution_artifact(release, release_channel="STABLE")
        self.assertEqual(artifact["release_channel"], "STABLE")
        self.assertEqual(
            artifact["content"]["release"]["artifact"]["passport"]["evidence_digest"],
            passport.evidence_digest,
        )

    def test_passport_producer_rejects_private_or_insufficient_observations(self) -> None:
        payload = qualified_passport_observations()
        payload["observations"] = payload["observations"][:9]
        with self.assertRaisesRegex(ValueError, "10 to 512"):
            build_qualified_blueprint_passport(payload)
        payload = qualified_passport_observations()
        payload["observations"][0]["prompt"] = "do not publish this"
        with self.assertRaisesRegex(ValueError, "unsupported shape"):
            build_qualified_blueprint_passport(payload)

    def test_generic_artifact_runtime_cannot_activate_community_graph(self) -> None:
        publication = CommunityBlueprintRegistry()
        publication.prepare(local_blueprint(), draft_id="release_share", artifact_id="release_review")
        publication.publish("release_share")
        artifact = community_release_to_evolution_artifact(
            community_release_from_payload(publication.export_release("release_share"))
        )
        with tempfile.TemporaryDirectory() as directory:
            with EvolutionStore(Path(directory) / "evolution.sqlite3") as store:
                service = EvolutionNetworkService(store)
                service.register_artifact_manifest(artifact)
                with self.assertRaisesRegex(ValueError, "community-import-reviewed"):
                    service.stage_artifact("release_review", "0.0.1")

    def test_cli_lifecycle_requires_explicit_publication_steps_and_stages_before_activation(self) -> None:
        def invoke(arguments: list[str]) -> dict[str, object]:
            output, error = io.StringIO(), io.StringIO()
            code = main(arguments, stdout=output, stderr=error)
            self.assertEqual(code, EXIT_OK, error.getvalue())
            return json.loads(output.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "company.sqlite3"
            local = root / "local.json"
            release = root / "release.json"
            local.write_text(
                json.dumps(local_blueprint().canonical_payload()), encoding="utf-8"
            )
            invoke(["graph", "import", str(local), "--state", str(state), "--confirm", "--json"])
            prepared = invoke([
                "graph", "community-prepare", "private_release_review", "1",
                "release_share", "release_review", "--state", str(state), "--confirm", "--json",
            ])
            self.assertEqual(prepared["community_publication"]["state"], "DRAFT")
            invoke(["graph", "community-publish", "release_share", "--state", str(state), "--confirm", "--json"])
            exported = invoke([
                "graph", "community-export", "release_share", str(release),
                "--state", str(state), "--confirm", "--json",
            ])
            self.assertEqual(exported["release_path"], str(release.resolve()))
            artifact_file = root / "release-artifact.json"
            packaged = invoke([
                "graph", "community-artifact-export", "release_share", str(artifact_file),
                "--state", str(state), "--confirm", "--json",
            ])
            self.assertEqual(packaged["artifact"]["kind"], "GRAPH_BLUEPRINT")
            inspected_artifact = invoke([
                "graph", "community-artifact-inspect", str(artifact_file), "--json",
            ])
            self.assertEqual(inspected_artifact["runtime_effect"], "NONE")
            with patch(
                "dynamic_firm.cli.SQLiteGraphBlueprintRegistry",
                side_effect=AssertionError("inspect must not open a local registry"),
            ):
                inspected = invoke(["graph", "community-inspect", str(release), "--json"])
            self.assertNotIn("private_release_review", json.dumps(inspected))
            staged = invoke([
                "graph", "community-stage", str(release), "--state", str(state), "--confirm", "--json",
            ])
            self.assertEqual(staged["staged_blueprint"]["origin"], "STAGED_COMMUNITY")
            activated = invoke([
                "graph", "community-activate", "community_release_review", "1",
                "--state", str(state), "--confirm", "--json",
            ])
            self.assertEqual(
                activated["selection"]["blueprint_ref"]["blueprint_id"],
                "community_release_review",
            )

    def test_cli_produces_then_binds_a_qualified_passport(self) -> None:
        def invoke(arguments: list[str]) -> dict[str, object]:
            output, error = io.StringIO(), io.StringIO()
            code = main(arguments, stdout=output, stderr=error)
            self.assertEqual(code, EXIT_OK, error.getvalue())
            return json.loads(output.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "company.sqlite3"
            local = root / "local.json"
            observations = root / "observations.json"
            passport = root / "passport.json"
            local.write_text(json.dumps(local_blueprint().canonical_payload()), encoding="utf-8")
            observations.write_text(json.dumps(qualified_passport_observations()), encoding="utf-8")
            invoke(["graph", "community-passport-build", str(observations), str(passport), "--confirm", "--json"])
            invoke(["graph", "import", str(local), "--state", str(state), "--confirm", "--json"])
            prepared = invoke([
                "graph", "community-prepare", "private_release_review", "1", "release_with_passport",
                "release_review_with_passport", "--passport", str(passport), "--state", str(state),
                "--confirm", "--json",
            ])
            self.assertEqual(
                prepared["community_publication"]["release"]["artifact"]["passport"]["sample_count"],
                10,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
