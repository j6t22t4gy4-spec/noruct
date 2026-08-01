from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dynamic_firm.company.cross_store_receipts import (
    BoundaryReceipt,
    CrossStoreReceiptChain,
    ReceiptBoundary,
    ReceiptPhase,
    ReceiptReplayStatus,
)


class CrossStoreReceiptTests(unittest.TestCase):
    parent_id = "organization-chain-1"

    def receipt(self, boundary: ReceiptBoundary, phase: ReceiptPhase, *, effect_observed: bool = False) -> BoundaryReceipt:
        digest_character = {
            ReceiptBoundary.FIT: "a",
            ReceiptBoundary.PLAN: "b",
            ReceiptBoundary.ASSIGNMENT: "c",
            ReceiptBoundary.GRAPH: "d",
            ReceiptBoundary.LEASE: "e",
            ReceiptBoundary.TERMINAL_SUMMARY: "f",
        }[boundary]
        return BoundaryReceipt(
            parent_id=self.parent_id,
            boundary=boundary,
            source_id=f"source-{boundary.value.lower()}",
            source_digest=digest_character * 64,
            phase=phase,
            effect_observed=effect_observed,
        )

    def test_success_chain_replays_complete_without_shared_transaction(self) -> None:
        chain = CrossStoreReceiptChain(parent_id=self.parent_id)
        for boundary in ReceiptBoundary:
            chain = chain.append(self.receipt(boundary, ReceiptPhase.PREPARED))
            chain = chain.append(self.receipt(boundary, ReceiptPhase.COMMITTED))

        replay = chain.replay()

        self.assertEqual(replay.status, ReceiptReplayStatus.COMPLETE)
        self.assertEqual(set(replay.committed_boundaries), set(ReceiptBoundary))

    def test_crash_before_commit_is_pending(self) -> None:
        chain = CrossStoreReceiptChain(parent_id=self.parent_id).append(
            self.receipt(ReceiptBoundary.FIT, ReceiptPhase.PREPARED)
        )

        replay = chain.replay()

        self.assertEqual(replay.status, ReceiptReplayStatus.PENDING)
        self.assertEqual(replay.pending_boundaries, (ReceiptBoundary.FIT,))

    def test_crash_after_effect_is_unknown_and_authority_conflict_is_rejected(self) -> None:
        chain = CrossStoreReceiptChain(parent_id=self.parent_id).append(
            self.receipt(ReceiptBoundary.LEASE, ReceiptPhase.PREPARED, effect_observed=True)
        )
        self.assertEqual(chain.replay().status, ReceiptReplayStatus.UNKNOWN)

        with self.assertRaises(ValueError):
            chain.append(
                BoundaryReceipt(
                    parent_id=self.parent_id,
                    boundary=ReceiptBoundary.LEASE,
                    source_id="other-authority",
                    source_digest="f" * 64,
                    phase=ReceiptPhase.COMMITTED,
                )
            )


if __name__ == "__main__":
    unittest.main()
