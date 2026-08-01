from __future__ import annotations

import argparse
import io
import json
import unittest

from dynamic_firm.application.foundation_cli.command import run_foundation_command


class FoundationCliAdapterTests(unittest.TestCase):
    def test_status_dispatch_returns_the_structured_foundation_contract(self) -> None:
        output = io.StringIO()

        result = run_foundation_command(
            argparse.Namespace(foundation_command="status", json=True),
            output,
            exit_ok=0,
        )

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertIn("source", payload)
        self.assertIn("dependencies", payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
