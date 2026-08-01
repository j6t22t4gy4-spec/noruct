from __future__ import annotations

import unittest

from dynamic_firm.company.local_posterior import (
    CentralPrior, LocalPosteriorEvidence, PosteriorPolicy, PosteriorStatus, resolve_local_posterior,
)


class LocalPosteriorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prior = CentralPrior("coding", 0.60, 0.10)
        self.policy = PosteriorPolicy(minimum_sample=4, maximum_uncertainty=0.20, maximum_disagreement=0.20, maximum_adjustment=0.05)

    def test_sparse_conflicting_and_high_uncertainty_preserve_prior(self) -> None:
        fixtures = (
            (LocalPosteriorEvidence("coding", 0, 0.90, 0.10), PosteriorStatus.INSUFFICIENT_SAMPLE),
            (LocalPosteriorEvidence("coding", 5, 0.95, 0.10), PosteriorStatus.CONFLICTING_EVIDENCE),
            (LocalPosteriorEvidence("coding", 5, 0.64, 0.30), PosteriorStatus.HIGH_UNCERTAINTY),
        )
        for evidence, status in fixtures:
            with self.subTest(status=status):
                result = resolve_local_posterior(self.prior, evidence, self.policy)
                self.assertEqual(result.status, status)
                self.assertEqual(result.score, self.prior.score)

    def test_absent_or_negative_transfer_evidence_never_promotes(self) -> None:
        self.assertEqual(resolve_local_posterior(self.prior, None, self.policy).status, PosteriorStatus.NO_LOCAL_EVIDENCE)
        result = resolve_local_posterior(self.prior, LocalPosteriorEvidence("research", 9, 0.99, 0.01), self.policy)
        self.assertEqual(result.status, PosteriorStatus.TASK_CLASS_MISMATCH)
        self.assertEqual(result.score, self.prior.score)

    def test_sufficient_agreement_has_only_a_bounded_nonselecting_correction(self) -> None:
        result = resolve_local_posterior(self.prior, LocalPosteriorEvidence("coding", 8, 0.68, 0.10), self.policy)
        self.assertTrue(result.correction_applied)
        self.assertEqual(result.score, 0.65)

    def test_malformed_numbers_fail_closed(self) -> None:
        with self.assertRaises(ValueError): CentralPrior("coding", float("nan"), 0.1)
        with self.assertRaises(ValueError): PosteriorPolicy(maximum_adjustment=float("inf"))
