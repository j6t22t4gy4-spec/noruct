from __future__ import annotations

import io
import json
import unittest
from types import SimpleNamespace

from dynamic_firm.cli import EXIT_OK, _run_capabilities_command


class CapabilityStatusTests(unittest.TestCase):
    def test_catalog_reports_global_authority_and_withheld_configured_lane(self) -> None:
        output = io.StringIO()
        result = _run_capabilities_command(
            SimpleNamespace(json=True),
            {
                "provider": {"kind": "openai_codex", "codex_command": "codex"},
                "run": {
                    "external_read_mode": "blocked",
                    "external_state_mode": "blocked",
                },
                "web_search": {
                    "enabled": True,
                    "base_url": "http://127.0.0.1:8080",
                },
            },
            output,
        )
        payload = json.loads(output.getvalue())

        self.assertEqual(result, EXIT_OK)
        self.assertEqual(payload["global_authority"]["external_read"], "blocked")
        self.assertEqual(payload["web_search"]["lifecycle"], "withheld")
