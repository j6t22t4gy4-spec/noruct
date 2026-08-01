from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dynamic_firm.company import (
    CompanyStateStore,
    EvolutionAutonomyMode,
    RetentionReviewMode,
)


class EvolutionAutonomyTests(unittest.TestCase):
    def test_defaults_to_propose_and_versions_the_single_user_choice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with CompanyStateStore(Path(directory) / "company.db") as store:
                self.assertEqual(store.evolution_autonomy_mode(), EvolutionAutonomyMode.PROPOSE)
                before = store.company().revision
                company, changed = store.set_evolution_autonomy_mode(
                    EvolutionAutonomyMode.ALWAYS_APPROVE,
                    actor="user:test",
                )
                self.assertTrue(changed)
                self.assertEqual(company.revision, before + 1)
                self.assertEqual(store.evolution_autonomy_mode(), EvolutionAutonomyMode.ALWAYS_APPROVE)
                self.assertEqual(store.retention_review_mode(), RetentionReviewMode.ALWAYS_APPROVE)
                self.assertTrue(store.company().policies["automatic_patch_apply"])
                self.assertTrue(store.company().policies["background_curator"])

                same, changed = store.set_evolution_autonomy_mode(
                    EvolutionAutonomyMode.ALWAYS_APPROVE,
                    actor="user:test",
                )
                self.assertFalse(changed)
                self.assertEqual(same.revision, company.revision)

                company, changed = store.set_evolution_autonomy_mode(
                    EvolutionAutonomyMode.NEVER,
                    actor="user:test",
                )
                self.assertTrue(changed)
                self.assertEqual(store.retention_review_mode(), RetentionReviewMode.APPROVAL)
                self.assertFalse(company.policies["automatic_patch_apply"])
                self.assertFalse(company.policies["background_curator"])

