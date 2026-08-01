from __future__ import annotations

import io
import json
import unittest

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.evaluation.tui_acceptance import (
    TuiAcceptanceScenario,
    run_tui_acceptance,
)
from dynamic_firm.product.terminal import display_width, strip_ansi


class TuiAcceptanceTests(unittest.TestCase):
    def test_three_gate_a_scenarios_pass_machine_contracts(self) -> None:
        records = tuple(run_tui_acceptance(scenario) for scenario in TuiAcceptanceScenario)

        self.assertTrue(all(record.machine_passed for record in records))
        self.assertTrue(all(record.human_review_required for record in records))
        self.assertTrue(all(not record.quota_consumed for record in records))
        conversation = records[0].rendered
        self.assertNotIn("Company plan", conversation)
        self.assertNotIn("Compiler", conversation)

    def test_preview_is_bounded_at_minimum_supported_width(self) -> None:
        for scenario in TuiAcceptanceScenario:
            with self.subTest(scenario=scenario):
                record = run_tui_acceptance(scenario, width=40)
                self.assertTrue(record.machine_passed)
                for line in strip_ansi(record.rendered).splitlines():
                    self.assertLessEqual(display_width(line), 40, line)

    def test_cli_renders_all_scenarios_or_stable_json_without_a_provider(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        exit_code = main(
            ["eval", "tui", "all", "--width", "80", "--json"],
            stdout=output,
            stderr=error,
        )
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, EXIT_OK, error.getvalue())
        self.assertEqual([item["scenario"] for item in payload], ["conversation", "solo", "approval"])
        self.assertTrue(all(item["machine_passed"] for item in payload))
        self.assertTrue(all(item["human_review_required"] for item in payload))
        self.assertTrue(all(not item["quota_consumed"] for item in payload))

    def test_plain_preview_has_no_terminal_control_or_box_characters(self) -> None:
        record = run_tui_acceptance("approval", plain=True)

        self.assertTrue(record.machine_passed)
        self.assertNotIn("\x1b", record.rendered)
        for character in "╭╮╰╯├┤│─":
            self.assertNotIn(character, record.rendered)


if __name__ == "__main__":
    unittest.main()
