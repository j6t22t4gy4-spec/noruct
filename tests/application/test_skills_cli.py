from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.application.skills_cli import run_skills_command
from dynamic_firm.application.skills_cli_parser import add_skills_commands


class SkillsCliAdapterTests(unittest.TestCase):
    def test_read_only_catalog_uses_only_explicit_skill_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            skill = root / "reviewer" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\nname: reviewer\ndescription: Review bounded evidence\n---\n"
                "# Reviewer\nInspect bounded evidence before reporting.\n",
                encoding="utf-8",
            )
            parser = argparse.ArgumentParser()
            commands = parser.add_subparsers(dest="command", required=True)
            add_skills_commands(commands)
            args = parser.parse_args(
                ["skills", "list", "--skills-dir", str(root), "--json"]
            )
            args.config = Path(temporary) / "noruct.toml"
            output = io.StringIO()

            result = run_skills_command(
                args,
                {},
                output,
                state_path_for=lambda _args, _settings: Path(temporary) / "runtime.db",
                exit_ok=0,
                exit_runtime=3,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["discovered_count"], 1)
        self.assertEqual(payload["skills"][0]["name"], "reviewer")
        self.assertEqual(payload["execution"], "SKILL.md instructions only; linked files and executable content are not loaded")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
