from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm._vendor.hermes_agent.upstream.noruct_firm import fork_cli
from dynamic_firm._vendor.hermes_agent.upstream.noruct_firm import entrypoint


class ForkCliBridgeTests(unittest.TestCase):
    def test_bare_noruct_enters_company_surface_not_direct_fork(self) -> None:
        with patch("dynamic_firm.cli.main", return_value=17) as company_main:
            self.assertEqual(entrypoint.main([]), 17)
            company_main.assert_called_once_with([])

    def test_query_shorthand_stays_inside_company_ingress(self) -> None:
        with patch("dynamic_firm.cli.main", return_value=0) as company_main:
            self.assertEqual(entrypoint.main(["--query", "hello"]), 0)
            company_main.assert_called_once_with(["ask", "hello"])

    def test_product_global_config_option_does_not_bypass_company_ingress(self) -> None:
        with patch("dynamic_firm.cli.main", return_value=0) as company_main:
            self.assertEqual(entrypoint.main(["--config", "/tmp/noruct.toml", "setup"]), 0)
            company_main.assert_called_once_with(["--config", "/tmp/noruct.toml", "setup"])

    def test_global_noruct_provider_defaults_fill_fork_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[provider]\nkind = "openai_api"\nmodel = "local-model"\nbase_url = "http://127.0.0.1:9000/v1"\n',
                encoding="utf-8",
            )
            received: dict[str, object] = {}

            def fake_main(**kwargs):
                received.update(kwargs)
                return 0

            with patch.object(fork_cli, "_hermes_main", return_value=fake_main):
                self.assertEqual(fork_cli.main(["--config", str(path), "--query", "hello"]), 0)
            self.assertEqual(received["model"], "local-model")
            self.assertEqual(received["provider"], "openai")
            self.assertEqual(received["base_url"], "http://127.0.0.1:9000/v1")


if __name__ == "__main__":
    unittest.main()
