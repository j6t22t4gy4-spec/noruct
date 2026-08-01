from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_OK, main
from dynamic_firm.mcp_connector import McpActionConfig, McpReadOnlyConfig
from dynamic_firm.product.mcp_action_settings import write_mcp_action_settings
from dynamic_firm.product.mcp_settings import write_mcp_settings


class CapabilityCenterTests(unittest.TestCase):
    def test_connect_skills_appears_in_grouped_capability_guide_then_disconnects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills" / "review"
            root.mkdir(parents=True)
            (root / "SKILL.md").write_text(
                "---\nname: review\ndescription: Review a change\n---\nRead the diff.\n",
                encoding="utf-8",
            )
            config = Path(temporary) / "config.toml"
            connected = io.StringIO()
            self.assertEqual(
                main(
                    ["--config", str(config), "skills", "connect", str(root.parent), "--json"],
                    stdout=connected,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            self.assertEqual(json.loads(connected.getvalue())["discovered_count"], 1)

            guide = io.StringIO()
            self.assertEqual(
                main(
                    ["--config", str(config), "capabilities", "guide", "--json"],
                    stdout=guide,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            payload = json.loads(guide.getvalue())
            self.assertEqual(payload["external_skills"]["lifecycle"], "ready")
            skill_receipt = payload["external_skills"]["receipt"]
            self.assertEqual(skill_receipt["schema"], "noruct.capability-receipt.v1")
            self.assertEqual(skill_receipt["kind"], "EXTERNAL_SKILL")
            self.assertEqual(skill_receipt["artifacts"][0]["revision"], "sha256:" + skill_receipt["artifacts"][0]["package_manifest_sha256"][:16])
            self.assertEqual(payload["capability_packages"]["adapters"]["skills"]["status_key"], "external_skills")
            self.assertEqual(payload["capability_packages"]["trust"], "trusted")

            disconnected = io.StringIO()
            self.assertEqual(
                main(
                    ["--config", str(config), "skills", "disconnect", "--json"],
                    stdout=disconnected,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            self.assertTrue(json.loads(disconnected.getvalue())["configuration_changed"])
            self.assertTrue((root / "SKILL.md").is_file())
            after_disconnect = io.StringIO()
            self.assertEqual(
                main(
                    ["--config", str(config), "capabilities", "status", "--json"],
                    stdout=after_disconnect,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            skill_receipt = json.loads(after_disconnect.getvalue())["external_skills"]["receipt"]
            self.assertEqual(skill_receipt["state"], "NOT_CONFIGURED")
            self.assertEqual(skill_receipt["artifacts"], [])

    def test_mcp_read_and_action_have_nonsecret_common_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            read = McpReadOnlyConfig(
                python_command=Path(sys.executable),
                server_command=Path(sys.executable),
                tool_name="external_search",
            )
            action = McpActionConfig(
                python_command=Path(sys.executable),
                server_command=Path(sys.executable),
                tool_name="external_write",
            )
            write_mcp_settings(config, read)
            write_mcp_action_settings(config, action)

            guide = io.StringIO()
            self.assertEqual(
                main(
                    ["--config", str(config), "capabilities", "guide", "--json"],
                    stdout=guide,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            payload = json.loads(guide.getvalue())
            read_receipt = payload["external_context"]["receipt"]
            action_receipt = payload["external_action"]["receipt"]
            self.assertEqual(read_receipt["schema"], "noruct.capability-receipt.v1")
            self.assertEqual(read_receipt["kind"], "MCP_READ")
            self.assertEqual(len(read_receipt["binding_digest"]), 64)
            self.assertEqual(action_receipt["schema"], "noruct.capability-receipt.v1")
            self.assertEqual(action_receipt["kind"], "MCP_ACTION")
            self.assertTrue(action_receipt["configuration_digest"].startswith("sha256:"))
            self.assertFalse(action_receipt["automatic_replacement"])

            disabled = io.StringIO()
            self.assertEqual(
                main(
                    ["--config", str(config), "mcp", "disable", "--json"],
                    stdout=disabled,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            self.assertTrue(json.loads(disabled.getvalue())["configuration_changed"])
            after_disable = io.StringIO()
            self.assertEqual(
                main(
                    ["--config", str(config), "capabilities", "status", "--json"],
                    stdout=after_disable,
                    stderr=io.StringIO(),
                ),
                EXIT_OK,
            )
            self.assertEqual(
                json.loads(after_disable.getvalue())["external_context"]["receipt"]["state"],
                "NOT_CONFIGURED",
            )


if __name__ == "__main__":
    unittest.main()
