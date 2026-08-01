from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.product.models import ModelOption, filter_model_options, model_options


class ModelOptionsTests(unittest.TestCase):
    def test_codex_options_use_local_default_cache_priority_visibility_and_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            (codex_home / "config.toml").write_text('model = "gpt-current"\n', encoding="utf-8")
            (codex_home / "models_cache.json").write_text(
                json.dumps(
                    {
                        "models": [
                            {"slug": "gpt-later", "priority": 20},
                            {"slug": "gpt-current", "priority": 10},
                            {"slug": "gpt-first", "priority": 1},
                            {"slug": "gpt-hidden", "priority": 0, "visibility": "hide"},
                            {"slug": "gpt-first", "priority": 2},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            previous = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = str(codex_home)
            try:
                options = model_options("openai_codex", "gpt-current")
            finally:
                if previous is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous

        self.assertEqual(
            [option.model_id for option in options],
            ["codex-default", "gpt-current", "gpt-first", "gpt-later"],
        )
        self.assertEqual([option.model_id for option in options if option.current], ["gpt-current"])
        self.assertNotIn("gpt-hidden", [option.model_id for option in options])

    def test_non_codex_provider_keeps_current_and_custom_is_a_ui_action(self) -> None:
        options = model_options("anthropic_api", "claude-contract")

        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].model_id, "claude-contract")
        self.assertTrue(options[0].current)

    def test_model_search_uses_ranked_local_catalog_without_provider_access(self) -> None:
        options = (
            ModelOption("gpt-5-codex", "catalog"),
            ModelOption("gpt-4.1-mini", "catalog"),
            ModelOption("codex-default", "catalog", current=True),
        )

        matches = filter_model_options(options, "gpt cod")

        self.assertEqual([option.model_id for option in matches], ["gpt-5-codex"])

    def test_symlinked_codex_config_and_cache_are_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / "codex"
            codex_home.mkdir()
            real_config = root / "outside-config.toml"
            real_cache = root / "outside-cache.json"
            real_config.write_text('model = "must-not-load"\n', encoding="utf-8")
            real_cache.write_text(
                json.dumps({"models": [{"slug": "also-must-not-load", "priority": 1}]}),
                encoding="utf-8",
            )
            (codex_home / "config.toml").symlink_to(real_config)
            (codex_home / "models_cache.json").symlink_to(real_cache)
            previous = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = str(codex_home)
            try:
                options = model_options("openai_codex", "codex-default")
            finally:
                if previous is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous

        self.assertEqual([option.model_id for option in options], ["codex-default"])


if __name__ == "__main__":
    unittest.main()
