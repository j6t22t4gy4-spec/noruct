from __future__ import annotations

import stat
import tempfile
import tomllib
import unittest
from pathlib import Path

from dynamic_firm.product.setup import SetupConfig, write_setup_config


class SetupConfigTests(unittest.TestCase):
    def test_setup_writes_only_non_secret_configuration_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / ".noruct" / "config.toml"
            written = write_setup_config(
                target,
                SetupConfig(
                    base_url="http://127.0.0.1:11434/v1",
                    model="local-model",
                    api_key_env="LOCAL_MODEL_KEY",
                ),
            )
            payload = tomllib.loads(target.read_text(encoding="utf-8"))
            mode = stat.S_IMODE(target.stat().st_mode)

        self.assertEqual(written, target.resolve())
        self.assertEqual(payload["provider"]["api_key_env"], "LOCAL_MODEL_KEY")
        self.assertNotIn("api_key", payload["provider"])
        self.assertEqual(payload["run"]["cost_mode"], "standard")
        self.assertEqual(payload["run"]["permission_mode"], "ask")
        self.assertEqual(mode, 0o600)

    def test_existing_configuration_requires_explicit_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            target.write_text("original", encoding="utf-8")
            config = SetupConfig(base_url="http://localhost:1/v1", model="model")

            with self.assertRaises(FileExistsError):
                write_setup_config(target, config)
            write_setup_config(target, config, overwrite=True)

            self.assertIn("[provider]", target.read_text(encoding="utf-8"))

    def test_external_process_setup_persists_only_protocol_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            write_setup_config(
                target,
                SetupConfig(
                    provider_kind="external_exec",
                    external_command="provider-bridge",
                    model="subscription-model",
                ),
            )
            payload = tomllib.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["provider"], {"kind": "external_exec", "external_command": "provider-bridge", "model": "subscription-model", "request_timeout": 30.0, "stale_timeout": 90.0})

    def test_setup_preserves_every_existing_capability_table_when_overwriting_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "config.toml"
            target.write_text(
                "[provider]\nkind = \"openai_codex\"\ncodex_command = \"codex\"\n\n"
                "[run]\nstate = \"runtime.db\"\n\n"
                "[browser]\nnode_command = \"/usr/bin/node\"\ncdp_endpoint = \"http://127.0.0.1:9222\"\n\n"
                "[slack]\ntoken_env = \"SLACK_TOKEN\"\nchannel_id = \"C123\"\n",
                encoding="utf-8",
            )
            write_setup_config(
                target,
                SetupConfig(base_url="http://127.0.0.1:11434/v1", model="local-model", no_auth=True),
                overwrite=True,
            )
            payload = tomllib.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(payload["provider"]["model"], "local-model")
        self.assertEqual(payload["browser"]["cdp_endpoint"], "http://127.0.0.1:9222")
        self.assertEqual(payload["slack"]["channel_id"], "C123")


if __name__ == "__main__":
    unittest.main()
