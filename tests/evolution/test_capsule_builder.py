from __future__ import annotations

import json
import math
import unittest
from dataclasses import fields

from dynamic_firm.company.models import content_digest
from dynamic_firm.evolution import (
    ActiveJobCapsuleEvidence,
    BlueprintDeltaProposalEvidence,
    CapsuleAuthority,
    CapsuleCostBucket,
    CapsuleEvaluatorKind,
    CapsuleEvidenceSource,
    CapsuleExecutionEvidence,
    CapsuleOutcomeEvidence,
    CapsuleOutcomeStatus,
    CapsuleRiskLevel,
    CapsuleTaskEvidence,
    UnsafeCapsuleEvidenceError,
    build_learning_capsule,
    preview_learning_capsule,
)
from dynamic_firm.evolution.service import validate_capsule


SOURCE_DIGEST = "a" * 64


def evidence(**overrides: object) -> ActiveJobCapsuleEvidence:
    values: dict[str, object] = {
        "source": CapsuleEvidenceSource.ACTIVE_JOB_LEDGER,
        "source_record_digest": SOURCE_DIGEST,
        "capability": "repository_analysis",
        "authority": CapsuleAuthority.ORGANIZATION_OWNER,
        "task": CapsuleTaskEvidence(
            domain="software",
            operation="analyze",
            input_fields=("repository_shape",),
            risk_level=CapsuleRiskLevel.LOW,
        ),
        "execution": CapsuleExecutionEvidence(
            workflow_shape=("solo",),
            tool_classes=("workspace_read",),
            decision_count=2,
        ),
        "outcome": CapsuleOutcomeEvidence(
            status=CapsuleOutcomeStatus.SUCCEEDED,
            quality_score=0.8,
            cost_bucket=CapsuleCostBucket.LOW,
            evaluator_kind=CapsuleEvaluatorKind.LOCAL_TEST,
            metric_names=("acceptance_passed",),
        ),
    }
    values.update(overrides)
    return ActiveJobCapsuleEvidence(**values)  # type: ignore[arg-type]


def proposal() -> BlueprintDeltaProposalEvidence:
    return BlueprintDeltaProposalEvidence(
        blueprint_id="repository_researcher",
        base_version="1.0.0",
        candidate_version="1.1.0",
        alias="repository_inspection",
        target_capability="repository_analysis",
    )


class CapsuleBuilderTests(unittest.TestCase):
    def test_builds_validator_compatible_v1_capsule(self) -> None:
        capsule = build_learning_capsule(evidence())

        self.assertEqual(capsule, validate_capsule(capsule))
        self.assertEqual(capsule["schema"], "noruct.learning-capsule.v1")
        self.assertTrue(capsule["execution_summary"]["redaction_applied"])
        self.assertNotIn("source_record_digest", capsule)

    def test_builds_validator_compatible_typed_v2_proposal(self) -> None:
        capsule = build_learning_capsule(evidence(), proposal())

        self.assertEqual(capsule, validate_capsule(capsule))
        self.assertEqual(capsule["schema"], "noruct.learning-capsule.v2")
        self.assertEqual(capsule["proposal"]["kind"], "BLUEPRINT_DELTA")
        self.assertEqual(
            capsule["proposal"]["delta"]["rollback"],
            {"kind": "CAPABILITY_ALIAS_REMOVE", "alias": "repository_inspection"},
        )

    def test_preview_is_content_free_and_digest_bound(self) -> None:
        record = evidence()
        capsule = build_learning_capsule(record)
        preview = preview_learning_capsule(record)
        encoded = json.dumps(preview, sort_keys=True)

        self.assertEqual(preview["payload_digest"], content_digest(capsule))
        self.assertEqual(preview["source_evidence"]["digest"], SOURCE_DIGEST)
        self.assertIn("task_schema.domain", preview["included_fields"])
        self.assertIn("prompt", preview["excluded_fields"])
        self.assertNotIn("sanitized_capsule", preview)
        self.assertNotIn("private user prompt", encoded)

    def test_preview_includes_only_proposal_field_names_not_values(self) -> None:
        preview = preview_learning_capsule(evidence(), proposal())
        encoded = json.dumps(preview, sort_keys=True)

        self.assertIn("proposal.delta.alias", preview["included_fields"])
        self.assertNotIn("repository_inspection", encoded)
        self.assertNotIn("repository_researcher", encoded)

    def test_input_contract_has_no_raw_payload_field(self) -> None:
        contract_fields = {
            item.name
            for record in (
                ActiveJobCapsuleEvidence,
                CapsuleTaskEvidence,
                CapsuleExecutionEvidence,
                CapsuleOutcomeEvidence,
                BlueprintDeltaProposalEvidence,
            )
            for item in fields(record)
        }
        forbidden = {
            "prompt",
            "messages",
            "transcript",
            "code",
            "path",
            "memory",
            "credentials",
            "token",
            "raw_output",
            "payload",
        }

        self.assertFalse(contract_fields & forbidden)

    def test_builder_rejects_mapping_instead_of_typed_record(self) -> None:
        with self.assertRaisesRegex(UnsafeCapsuleEvidenceError, "exact ActiveJobCapsuleEvidence"):
            build_learning_capsule({"prompt": "private"})  # type: ignore[arg-type]

    def test_task_rejects_raw_field_name(self) -> None:
        with self.assertRaisesRegex(UnsafeCapsuleEvidenceError, "raw/private field"):
            CapsuleTaskEvidence(
                domain="software",
                operation="analyze",
                input_fields=("prompt",),
                risk_level=CapsuleRiskLevel.LOW,
            )

    def test_task_rejects_path_like_identifier(self) -> None:
        with self.assertRaisesRegex(UnsafeCapsuleEvidenceError, "normalized lower-case identifier"):
            CapsuleTaskEvidence(
                domain="software",
                operation="analyze",
                input_fields=("/users/alice/private.py",),
                risk_level=CapsuleRiskLevel.LOW,
            )

    def test_task_rejects_email_value(self) -> None:
        with self.assertRaisesRegex(UnsafeCapsuleEvidenceError, "normalized lower-case identifier"):
            CapsuleTaskEvidence(
                domain="alice@example.com",
                operation="analyze",
                input_fields=("repository_shape",),
                risk_level=CapsuleRiskLevel.LOW,
            )

    def test_scanner_rejects_secret_shaped_identifier(self) -> None:
        with self.assertRaisesRegex(UnsafeCapsuleEvidenceError, "private or secret"):
            evidence(capability="sk-abcdefghijklmnop")

        with self.assertRaisesRegex(UnsafeCapsuleEvidenceError, "private or secret"):
            evidence(capability="customer_access_token")

    def test_builder_revalidates_a_low_level_mutated_frozen_record(self) -> None:
        record = evidence()
        object.__setattr__(record, "capability", "customer_access_token")

        with self.assertRaisesRegex(UnsafeCapsuleEvidenceError, "private or secret"):
            build_learning_capsule(record)

    def test_source_digest_is_strict_lowercase_sha256(self) -> None:
        for bad in ("ledger-123", "A" * 64, "a" * 63, "a" * 65):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(UnsafeCapsuleEvidenceError, "SHA-256"):
                    evidence(source_record_digest=bad)

    def test_enums_are_required_not_untyped_strings(self) -> None:
        with self.assertRaisesRegex(UnsafeCapsuleEvidenceError, "exact CapsuleRiskLevel"):
            CapsuleTaskEvidence(
                domain="software",
                operation="analyze",
                input_fields=("repository_shape",),
                risk_level="LOW",  # type: ignore[arg-type]
            )

    def test_identifier_collections_must_be_tuples(self) -> None:
        with self.assertRaisesRegex(UnsafeCapsuleEvidenceError, "must be a tuple"):
            CapsuleExecutionEvidence(
                workflow_shape=["solo"],  # type: ignore[arg-type]
                tool_classes=("workspace_read",),
                decision_count=2,
            )

    def test_identifier_collections_reject_duplicates(self) -> None:
        with self.assertRaisesRegex(UnsafeCapsuleEvidenceError, "duplicate"):
            CapsuleExecutionEvidence(
                workflow_shape=("solo", "solo"),
                tool_classes=("workspace_read",),
                decision_count=2,
            )

    def test_decision_count_rejects_bool_and_out_of_bounds(self) -> None:
        for bad in (True, -1, 10_001):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(UnsafeCapsuleEvidenceError, "0 to 10000"):
                    CapsuleExecutionEvidence(
                        workflow_shape=("solo",),
                        tool_classes=("workspace_read",),
                        decision_count=bad,  # type: ignore[arg-type]
                    )

    def test_quality_score_is_finite_bounded_and_canonical(self) -> None:
        for bad in (True, -0.1, 1.1, math.nan, math.inf, 1e-7, -0.0, 0.001):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(
                    UnsafeCapsuleEvidenceError,
                    "must be a finite number from 0 to 1",
                ):
                    CapsuleOutcomeEvidence(
                        status=CapsuleOutcomeStatus.SUCCEEDED,
                        quality_score=bad,  # type: ignore[arg-type]
                        cost_bucket=CapsuleCostBucket.LOW,
                        evaluator_kind=CapsuleEvaluatorKind.LOCAL_TEST,
                        metric_names=("acceptance_passed",),
                    )
        for accepted in (0.0, 0.8, 0.95, 1.0):
            with self.subTest(accepted=accepted):
                outcome = CapsuleOutcomeEvidence(
                    status=CapsuleOutcomeStatus.SUCCEEDED,
                    quality_score=accepted,
                    cost_bucket=CapsuleCostBucket.LOW,
                    evaluator_kind=CapsuleEvaluatorKind.LOCAL_TEST,
                    metric_names=("acceptance_passed",),
                )
                self.assertEqual(outcome.quality_score, accepted)

    def test_proposal_requires_semver_and_changed_version(self) -> None:
        for base, candidate in (("main", "1.1.0"), ("1.0.0", "1.0.0")):
            with self.subTest(base=base, candidate=candidate):
                with self.assertRaises(UnsafeCapsuleEvidenceError):
                    BlueprintDeltaProposalEvidence(
                        blueprint_id="repository_researcher",
                        base_version=base,
                        candidate_version=candidate,
                        alias="repository_inspection",
                        target_capability="repository_analysis",
                    )

    def test_company_episode_source_is_supported(self) -> None:
        capsule = build_learning_capsule(
            evidence(source=CapsuleEvidenceSource.COMPANY_EPISODE)
        )
        preview = preview_learning_capsule(
            evidence(source=CapsuleEvidenceSource.COMPANY_EPISODE)
        )

        self.assertEqual(capsule["schema"], "noruct.learning-capsule.v1")
        self.assertEqual(preview["source_evidence"]["kind"], "COMPANY_EPISODE")


if __name__ == "__main__":
    unittest.main()
