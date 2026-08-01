from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.assignment_rationale import (  # noqa: E402
    AssignmentAlternative,
    AssignmentExclusionReason,
    AssignmentRationale,
)
from dynamic_firm.kernel.models import (  # noqa: E402
    GraphPatch,
    GraphPatchProposalEvent,
    GraphPatchProposalStatus,
    GraphPatchOperation,
    PatchOperationKind,
    SemanticOperation,
    GraphMutationLease,
)
from dynamic_firm.product.material_alternatives import (  # noqa: E402
    MATERIAL_ALTERNATIVES_SCHEMA,
    MaterialAlternativeEntry,
    MaterialAlternativeLedger,
    MaterialAlternativeStatus,
)


class MaterialAlternativeRoundTripTests(unittest.TestCase):
    def test_evaluated_entry_round_trip(self) -> None:
        ledger = MaterialAlternativeLedger(
            (MaterialAlternativeEntry("SOLO", MaterialAlternativeStatus.EVALUATED),)
        )

        restored = MaterialAlternativeLedger.from_payload(ledger.payload())

        self.assertEqual(restored, ledger)
        self.assertTrue(restored.entries[0].compared)

    def test_rejected_b06_entry_round_trip(self) -> None:
        rationale = AssignmentRationale(
            required_capability="capability.review",
            selected_material_profile_digest="a" * 64,
            exercised_capability="capability.review",
            alternatives=(
                AssignmentAlternative.compared_candidate(
                    alternative_id="TEAM",
                    material_profile_digest="b" * 64,
                    exclusion_reason=AssignmentExclusionReason.EVIDENCE_WEAK,
                ),
            ),
        )

        ledger = MaterialAlternativeLedger.from_b06(rationale)
        restored = MaterialAlternativeLedger.from_payload(ledger.payload())

        self.assertEqual(restored, ledger)
        self.assertEqual(restored.entries[0].status, MaterialAlternativeStatus.REJECTED)
        self.assertTrue(restored.entries[0].compared)

    def test_not_evaluated_graph_proposal_round_trip(self) -> None:
        patch = GraphPatch(
            patch_id="patch-1",
            base_graph_version=1,
            trigger_task_id="task-1",
            semantic_operation=SemanticOperation.INSERT,
            rationale="bounded",
            expected_gain="coverage",
            operations=(
                GraphPatchOperation(kind=PatchOperationKind.CANCEL_TASK, task_id="task-1"),
            ),
        )
        event = GraphPatchProposalEvent(
            proposal_id="proposal-1",
            event_id="event-1",
            patch=patch,
            before_graph_digest="before",
            after_graph_digest="after",
            proposed_lease=GraphMutationLease(),
            status=GraphPatchProposalStatus.UNAVAILABLE,
            content_hash="content",
        )

        ledger = MaterialAlternativeLedger.from_graph_proposal(event)
        restored = MaterialAlternativeLedger.from_payload(ledger.payload())

        self.assertEqual(restored, ledger)
        entry = restored.entries[0]
        self.assertEqual(entry.status, MaterialAlternativeStatus.NOT_EVALUATED)
        self.assertFalse(entry.compared)
        self.assertEqual(entry.decision, "UNAVAILABLE")
        self.assertEqual(ledger.payload()["schema_version"], MATERIAL_ALTERNATIVES_SCHEMA)

    def test_rejected_graph_proposal_uses_only_fixed_unknown_reason(self) -> None:
        patch = GraphPatch(
            patch_id="patch-rejected",
            base_graph_version=1,
            trigger_task_id="task-1",
            semantic_operation=SemanticOperation.INSERT,
            rationale="bounded",
            expected_gain="coverage",
            operations=(
                GraphPatchOperation(kind=PatchOperationKind.CANCEL_TASK, task_id="task-1"),
            ),
        )
        event = GraphPatchProposalEvent(
            proposal_id="proposal-rejected",
            event_id="event-rejected",
            patch=patch,
            before_graph_digest="before",
            after_graph_digest="after",
            proposed_lease=GraphMutationLease(),
            status=GraphPatchProposalStatus.REJECTED,
            content_hash="content",
        )

        entry = MaterialAlternativeLedger.from_graph_proposal(event).entries[0]

        self.assertEqual(entry.status, MaterialAlternativeStatus.REJECTED)
        self.assertEqual(entry.reason, AssignmentExclusionReason.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
