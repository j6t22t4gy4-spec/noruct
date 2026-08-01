from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from dynamic_firm.company.organization_transition import (
    OrganizationTransition,
    OrganizationTransitionProposal,
    OrganizationTransitionReceipt,
    adapt_kernel_graph_proposal_receipt,
)
from dynamic_firm.kernel.models import (
    GraphMutationLease,
    GraphPatch,
    GraphPatchProposalStatus,
    JobGraph,
    SemanticOperation,
)
from dynamic_firm.kernel.mutation import graph_patch_proposal_event


class OrganizationTransitionTests(unittest.TestCase):
    def _proposal_and_kernel_receipt(self):
        lease = GraphMutationLease(model_calls=2, tool_calls=1, cost_usd=0.25)
        before = JobGraph(version=3, tasks=(), final_task_id="final")
        after = JobGraph(version=4, tasks=(), final_task_id="final")
        patch = GraphPatch(
            patch_id="patch-1",
            base_graph_version=3,
            trigger_task_id="trigger",
            semantic_operation=SemanticOperation.SPLIT,
            rationale="independent value evidence",
            expected_gain="bounded parallel verification",
            operations=(),
        )
        event = graph_patch_proposal_event(
            patch=patch,
            before=before,
            after=after,
            proposed_lease=lease,
            status=GraphPatchProposalStatus.APPROVED,
        )
        proposal = OrganizationTransitionProposal(
            transition=OrganizationTransition.SOLO_TO_SPLIT,
            reason="independent value evidence",
            expected_benefit="bounded parallel verification",
            incremental_lease=lease,
            verification_plan="compare the split outputs independently",
            stop_condition="stop on failed independence or unknown result",
            rollback_or_replacement_boundary="replace the candidate before Kernel commit",
        )
        return proposal, event, before, after

    def test_binds_exact_kernel_receipt_without_applying_graph_change(self):
        proposal, event, before, after = self._proposal_and_kernel_receipt()

        receipt = adapt_kernel_graph_proposal_receipt(proposal, event)

        self.assertIsInstance(receipt, OrganizationTransitionReceipt)
        self.assertIs(receipt.proposal, proposal)
        self.assertIs(receipt.kernel_proposal, event)
        self.assertEqual(before.version, 3)
        self.assertEqual(after.version, 4)

    def test_proposal_and_receipt_are_immutable(self):
        proposal, event, _, _ = self._proposal_and_kernel_receipt()
        receipt = adapt_kernel_graph_proposal_receipt(proposal, event)

        with self.assertRaises(FrozenInstanceError):
            proposal.reason = "changed"
        with self.assertRaises(FrozenInstanceError):
            receipt.kernel_proposal = event

    def test_rejects_malformed_or_authority_expanding_inputs(self):
        proposal, event, _, _ = self._proposal_and_kernel_receipt()

        with self.assertRaises(TypeError):
            OrganizationTransitionProposal(
                transition="SOLO_TO_SPLIT",
                reason=proposal.reason,
                expected_benefit=proposal.expected_benefit,
                incremental_lease=proposal.incremental_lease,
                verification_plan=proposal.verification_plan,
                stop_condition=proposal.stop_condition,
                rollback_or_replacement_boundary=proposal.rollback_or_replacement_boundary,
            )
        with self.assertRaises(TypeError):
            OrganizationTransitionProposal(
                transition=proposal.transition,
                reason=proposal.reason,
                expected_benefit=proposal.expected_benefit,
                incremental_lease=proposal.incremental_lease,
                verification_plan=proposal.verification_plan,
                stop_condition=proposal.stop_condition,
                rollback_or_replacement_boundary=proposal.rollback_or_replacement_boundary,
                apply=True,
            )
        with self.assertRaises(TypeError):
            adapt_kernel_graph_proposal_receipt(proposal, {})
        with self.assertRaises(ValueError):
            adapt_kernel_graph_proposal_receipt(
                proposal,
                replace(event, content_hash="0" * 64),
            )
        with self.assertRaises(ValueError):
            adapt_kernel_graph_proposal_receipt(
                replace(
                    proposal,
                    incremental_lease=GraphMutationLease(model_calls=99),
                ),
                event,
            )


if __name__ == "__main__":
    unittest.main()
