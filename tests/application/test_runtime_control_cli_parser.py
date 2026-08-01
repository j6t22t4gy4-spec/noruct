from __future__ import annotations

import argparse
import unittest

from dynamic_firm.application.runtime_control_cli_parser import (
    add_runtime_control_commands,
)


class RuntimeControlCliParserTests(unittest.TestCase):
    def test_registers_job_graph_and_data_lifecycles(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_runtime_control_commands(
            commands,
            add_execution_options=lambda command, **_kwargs: command.add_argument(
                "--workspace", default=None
            ),
        )

        run = parser.parse_args(["run", "goal"])
        data = parser.parse_args(["data", "delete", "--confirm"])

        self.assertEqual(run.command, "run")
        self.assertEqual((data.command, data.data_command), ("data", "delete"))

    def test_effect_resolution_requires_explicit_operator_inputs(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_runtime_control_commands(
            commands,
            add_execution_options=lambda command, **_kwargs: command.add_argument(
                "--workspace", default=None
            ),
        )

        args = parser.parse_args(
            [
                "job",
                "effect-resolve",
                "job-1",
                "action-1",
                "compensated",
                "--operator-id",
                "operator-1",
                "--reason",
                "compensation receipt verified",
                "--evidence-digest",
                "a" * 64,
                "--confirm",
                "--json",
            ]
        )

        self.assertEqual(args.job_command, "effect-resolve")
        self.assertEqual(args.outcome, "compensated")
        self.assertTrue(args.confirm)

    def test_registers_explicit_portfolio_reestimate_decision(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        add_runtime_control_commands(
            commands,
            add_execution_options=lambda command, **_kwargs: command.add_argument("--workspace", default=None),
        )
        args = parser.parse_args(
            [
                "portfolio", "reestimate", "decide", "estimate-1", "--choice", "CONTINUE",
                "--reason", "OPERATOR_CONTINUE", "--confirm",
            ]
        )
        self.assertEqual((args.portfolio_command, args.portfolio_reestimate_command), ("reestimate", "decide"))
        self.assertTrue(args.confirm)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
