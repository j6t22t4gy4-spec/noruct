from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dynamic_firm.knowledge import (
    AttributionStatus,
    ContentTrustClass,
    EpistemicStatus,
    KnowledgeExecutionOutcome,
    KnowledgeFirmBridge,
    KnowledgeStore,
    KnowledgeVault,
    OracleValidatorType,
    OutcomeVerdict,
    UserKnowledgeService,
    ValidatorIndependence,
)


class EpistemicControlTests(unittest.TestCase):
    @staticmethod
    def runtime(directory: str):
        store = KnowledgeStore(Path(directory) / "knowledge.db")
        service = UserKnowledgeService(
            store,
            KnowledgeVault(Path(directory) / "knowledge.vault"),
        )
        return store, KnowledgeFirmBridge(service)

    def test_evidence_exposes_status_trust_freshness_conflicts_and_unknowns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, bridge = self.runtime(directory)
            record = store.create_record(
                kind="FACT",
                statement="Competitor pricing changed this quarter.",
                epistemic_status=EpistemicStatus.DISPUTED,
                trust_class=ContentTrustClass.USER_ASSERTED,
                freshness_expires_at="2026-09-01T00:00:00+00:00",
                conflict_refs=("record:competing-price-source",),
                unknown_refs=("unknown:effective-date",),
            )
            intent = store.create_intent(
                goal="Assess pricing strategy",
                knowledge_query="competitor pricing",
                constraints=("Do not change pricing automatically.",),
                acceptance_criteria=("Produce a reviewable recommendation.",),
            )

            prepared = bridge.prepare(
                intent.intent_id,
                request_id="request-epistemic-projection",
                job_id="job-epistemic-projection",
                assumptions=("Public pricing remains comparable.",),
                unknown_refs=("unknown:customer-elasticity",),
                excluded_alternatives=("Immediate irreversible price change",),
            )

            annotation = store.epistemic_annotation("RECORD", record.record_id)
            assert annotation is not None
            self.assertEqual(annotation.epistemic_status, EpistemicStatus.DISPUTED)
            item = prepared.evidence_pack.items[0]
            self.assertEqual(item.epistemic_status, EpistemicStatus.DISPUTED)
            self.assertEqual(item.trust_class, ContentTrustClass.USER_ASSERTED)
            self.assertEqual(item.conflict_refs, ("record:competing-price-source",))
            self.assertIn("trust:USER_ASSERTED", item.retrieval_basis)
            self.assertIn("conflicts:1", item.retrieval_basis)
            self.assertIn("freshness:current", item.retrieval_basis)
            self.assertIn("retrieval_basis=", prepared.evidence_pack.runtime_projection())
            self.assertIn("unknown:effective-date", item.unknown_refs)
            self.assertIn(
                "unknown:customer-elasticity",
                prepared.decision_context.unknown_refs,
            )
            self.assertIn("unknown:effective-date", prepared.decision_context.unknown_refs)
            self.assertEqual(
                prepared.decision_context.constraints,
                ("Do not change pricing automatically.",),
            )
            self.assertFalse(prepared.oracle_contract.has_executable_oracle)
            self.assertEqual(
                prepared.execution_origin.decision_context_digest,
                prepared.decision_context.content_digest,
            )
            self.assertEqual(
                prepared.execution_origin.oracle_contract_digest,
                prepared.oracle_contract.content_digest,
            )
            prepared.decision_context.verify()
            prepared.oracle_contract.verify()
            store.close()

    def test_oracle_and_delayed_outcome_never_auto_promote_job_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, bridge = self.runtime(directory)
            store.create_record(
                kind="FACT",
                statement="A measured baseline conversion rate exists.",
            )
            intent = store.create_intent(
                goal="Test the pricing recommendation",
                knowledge_query="baseline conversion rate",
                acceptance_criteria=("Conversion does not decline beyond two percent.",),
            )
            prepared = bridge.prepare(
                intent.intent_id,
                request_id="request-oracle-outcome",
                job_id="job-oracle-outcome",
                observable_signals=("seven-day conversion delta",),
                observation_channel="analytics:experiment-42",
                validator_type=OracleValidatorType.EXTERNAL_EVIDENCE,
                independence_class=ValidatorIndependence.INDEPENDENT_SOURCE,
                feedback_due_at="2026-08-15T00:00:00+00:00",
                risk_class="MEDIUM",
                reversibility_class="REVERSIBLE",
            )
            self.assertTrue(prepared.oracle_contract.has_executable_oracle)

            completed = bridge.complete(
                prepared,
                KnowledgeExecutionOutcome(
                    job_id=prepared.binding.job_id,
                    status="SUCCEEDED",
                    summary="The recommendation was produced; real-world impact is pending.",
                ),
            )
            self.assertEqual(completed.outcome.verdict, OutcomeVerdict.NOT_YET_OBSERVED)
            self.assertIsNone(completed.outcome.observed_at)
            self.assertIsNotNone(completed.candidate)
            # A successful Job produces a review candidate and a pending
            # outcome, never an automatically accepted Knowledge fact.
            self.assertEqual(len(store.list_records()), 1)

            observed = store.observe_outcome(
                completed.outcome.outcome_id,
                verdict=OutcomeVerdict.INCONCLUSIVE,
                observed_signal="Traffic was below the experiment threshold.",
                source_ref="analytics:experiment-42:report-1",
                reviewer_ref="user:owner",
                confounders=("holiday traffic",),
                attribution_status=AttributionStatus.CONFOUNDED,
            )
            repeated = store.observe_outcome(
                completed.outcome.outcome_id,
                verdict=OutcomeVerdict.INCONCLUSIVE,
                observed_signal="Traffic was below the experiment threshold.",
                source_ref="analytics:experiment-42:report-1",
                reviewer_ref="user:owner",
                confounders=("holiday traffic",),
                attribution_status=AttributionStatus.CONFOUNDED,
            )
            self.assertEqual(observed, repeated)
            with self.assertRaisesRegex(ValueError, "different observation"):
                store.observe_outcome(
                    completed.outcome.outcome_id,
                    verdict=OutcomeVerdict.PASSED,
                    observed_signal="Changed claim",
                    source_ref="analytics:experiment-42:report-2",
                    reviewer_ref="user:owner",
                )
            store.close()


if __name__ == "__main__":
    unittest.main()
