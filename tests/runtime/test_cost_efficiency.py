from __future__ import annotations

import unittest

from dynamic_firm.runtime.cost_efficiency import CostEfficiencyProjector
from dynamic_firm.runtime.models import CostEfficiencyMode, ModelMessage


class CostEfficiencyProjectorTests(unittest.TestCase):
    def test_economy_projects_only_large_successful_tool_text(self) -> None:
        source = ("same noisy line\n" * 600) + "".join(
            f"unique evidence line {index:04d} with stable diagnostic detail\n"
            for index in range(500)
        )
        messages = (
            ModelMessage("system", "stable policy"),
            ModelMessage("tool", {"ok": True, "content": source, "error_code": None}, "read-1"),
            ModelMessage("tool", {"ok": False, "content": source, "error_code": "READ_FAILED"}, "read-2"),
        )

        result = CostEfficiencyProjector().project(
            messages, mode=CostEfficiencyMode.ECONOMY
        )

        self.assertTrue(result.applied)
        self.assertEqual(result.projected_message_count, 1)
        self.assertLess(result.chars_after, result.chars_before)
        self.assertEqual(messages[1].content["content"], source)
        projected = result.messages[1].content["content"]
        self.assertIn("previous line repeated", projected)
        self.assertIn("middle omitted from model context", projected)
        self.assertEqual(result.messages[2], messages[2])

    def test_standard_mode_and_short_success_output_are_exact_passthrough(self) -> None:
        short = ModelMessage("tool", {"ok": True, "content": "evidence", "error_code": None})
        source = (short,)

        standard = CostEfficiencyProjector().project(
            source, mode=CostEfficiencyMode.STANDARD
        )
        economy = CostEfficiencyProjector().project(
            source, mode=CostEfficiencyMode.ECONOMY
        )

        self.assertEqual(standard.messages, source)
        self.assertFalse(standard.applied)
        self.assertEqual(economy.messages, source)
        self.assertFalse(economy.applied)
