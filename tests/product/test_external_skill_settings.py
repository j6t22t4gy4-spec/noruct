from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from dynamic_firm.product.external_skill_settings import (
    remove_external_skill_settings,
    write_external_skill_settings,
)


class ExternalSkillSettingsTests(unittest.TestCase):
    def test_replaces_only_skills_table_and_preserves_runtime_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            root.mkdir()
            target = Path(temporary) / "config.toml"
            target.write_text("[provider]\nmodel = \"one\"\n\n[run]\npermission_mode = \"ask\"\n", encoding="utf-8")
            write_external_skill_settings(target, (root,))
            stored = tomllib.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(stored["provider"]["model"], "one")
        self.assertEqual(stored["skills"]["external_dirs"], [str(root.resolve())])

    def test_disconnect_removes_only_skills_table_and_never_touches_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            root.mkdir()
            target = Path(temporary) / "config.toml"
            target.write_text(
                "[provider]\nmodel = \"one\"\n\n[skills]\nexternal_dirs = [\"ignored\"]\n",
                encoding="utf-8",
            )

            self.assertTrue(remove_external_skill_settings(target))
            stored = tomllib.loads(target.read_text(encoding="utf-8"))
            self.assertTrue(root.is_dir())

        self.assertEqual(stored["provider"]["model"], "one")
        self.assertNotIn("skills", stored)
