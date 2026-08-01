from __future__ import annotations

import io
import json
import unittest

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.manager_value_contract import (
    ManagerValueArm,
    manager_value_qualification_contract,
)


class ManagerValueQualificationContractTests(unittest.TestCase):
    def test_contract_has_exact_four_way_slots_without_claiming_outcomes(self) -> None:
        contract = manager_value_qualification_contract()

        self.assertEqual(contract.arms, tuple(ManagerValueArm))
        self.assertEqual(len(contract.fixtures), 4)
        self.assertEqual(len(contract.exact_slots), 16)
        self.assertTrue(contract.live_campaign_implemented)
        self.assertFalse(contract.outcome_claimed)
        self.assertIn("total_model_call_and_wall_time_budget", contract.frozen_dimensions)
        self.assertIn("requested_and_granted_approval_count", contract.required_outcomes)
        self.assertIn(
            "reported_cost_or_same_model_call_proxy_and_latency",
            contract.required_outcomes,
        )

    def test_cli_exposes_the_contract_as_read_only(self) -> None:
        output = io.StringIO()
        error = io.StringIO()

        code = main(["eval", "manager-value-contract", "--json"], stdout=output, stderr=error)

        self.assertEqual(code, EXIT_OK, error.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(len(payload["exact_slots"]), 16)
        self.assertFalse(payload["outcome_claimed"])


if __name__ == "__main__":
    unittest.main()
