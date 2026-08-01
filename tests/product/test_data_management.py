from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dynamic_firm.cli import EXIT_INPUT, EXIT_OK, build_parser, main
from dynamic_firm.company import CompanyStateStore
from dynamic_firm.knowledge.store import knowledge_state_path, knowledge_vault_path
from dynamic_firm.product import create_support_bundle, export_state_database
from dynamic_firm.runtime.store import SCHEMA_VERSION as RUNTIME_SCHEMA_VERSION, RunStore


class DataManagementTests(unittest.TestCase):
    @staticmethod
    def _state(path: Path) -> None:
        with CompanyStateStore(path):
            pass
        runtime = RunStore(path)
        runtime.close()
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS private_fixture (
                    content TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO private_fixture(content) VALUES (?)",
                ("raw-user-secret-should-not-enter-support-bundle",),
            )
            connection.commit()
        finally:
            connection.close()

    def test_state_export_is_integrity_checked_and_contains_the_full_runtime_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.db"
            destination = root / "export.db"
            self._state(source)

            record = export_state_database(source, destination)

            self.assertEqual(record.integrity_check, "ok")
            self.assertEqual(record.data_scope, "runtime-company-state")
            self.assertTrue(record.sensitive_user_data_included)
            self.assertFalse(record.separate_knowledge_state_included)
            self.assertEqual(
                record.separate_knowledge_command,
                "noruct knowledge export DESTINATION --state STATE_DB",
            )
            self.assertGreater(record.bytes_written, 0)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            connection = sqlite3.connect(destination)
            try:
                value = connection.execute(
                    "SELECT content FROM private_fixture"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(
                value,
                "raw-user-secret-should-not-enter-support-bundle",
            )

    def test_support_bundle_redacts_configuration_and_excludes_raw_database_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.db"
            destination = root / "support.json"
            config_path = root / "config.toml"
            config_path.write_text("[provider]\nmodel='fixture'\n", encoding="utf-8")
            self._state(source)

            record = create_support_bundle(
                source,
                config_path,
                {
                    "provider": {
                        "model": "fixture",
                        "api_key": "sk-support-fixture-secret",
                        "codex_command": "/Users/example/private/bin/codex",
                        "windows_command": "C:\\Users\\example\\private\\codex.exe",
                        "log_hint": "inspect /Users/example/private/trace.log before retry",
                    }
                },
                destination,
            )
            content = destination.read_text(encoding="utf-8")

            self.assertTrue(record.secret_redaction_applied)
            self.assertFalse(record.raw_user_content_included)
            self.assertNotIn("sk-support-fixture-secret", content)
            self.assertNotIn("/Users/example/private/bin/codex", content)
            self.assertNotIn("C:\\Users\\example\\private\\codex.exe", content)
            self.assertNotIn("inspect /Users/example/private/trace.log before retry", content)
            self.assertNotIn(str(root), content)
            self.assertIn("«redacted:local-path»", content)
            self.assertNotIn("raw-user-secret-should-not-enter-support-bundle", content)
            payload = json.loads(content)
            self.assertEqual(payload["state"]["integrity_check"], "ok")
            self.assertEqual(payload["state"]["company_schema_version"], 9)
            self.assertEqual(
                payload["state"]["runtime_schema_version"],
                RUNTIME_SCHEMA_VERSION,
            )
            self.assertFalse(payload["privacy"]["raw_user_content_included"])
            self.assertFalse(payload["privacy"]["local_path_values_included"])

    def test_cli_data_lifecycle_requires_confirmation_for_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "runtime.db"
            exported = root / "export.db"
            support = root / "support.json"
            self._state(state)
            output = io.StringIO()
            error = io.StringIO()

            export_code = main(
                [
                    "data",
                    "export",
                    str(exported),
                    "--state",
                    str(state),
                    "--json",
                ],
                stdout=output,
                stderr=error,
            )
            support_code = main(
                [
                    "data",
                    "support-bundle",
                    str(support),
                    "--state",
                    str(state),
                    "--json",
                ],
                stdout=io.StringIO(),
                stderr=error,
            )
            denied = main(
                ["data", "delete", "--state", str(state)],
                stdout=io.StringIO(),
                stderr=error,
            )
            knowledge_database = knowledge_state_path(state)
            knowledge_vault = knowledge_vault_path(knowledge_database)
            knowledge_database.write_bytes(b"separate-knowledge-database")
            knowledge_vault.mkdir()
            (knowledge_vault / "preserved-object").write_bytes(b"knowledge")
            deleted_output = io.StringIO()
            deleted = main(
                [
                    "data",
                    "delete",
                    "--state",
                    str(state),
                    "--confirm",
                    "--json",
                ],
                stdout=deleted_output,
                stderr=error,
            )

            self.assertEqual(export_code, EXIT_OK, error.getvalue())
            export_payload = json.loads(output.getvalue())
            self.assertEqual(export_payload["data_scope"], "runtime-company-state")
            self.assertFalse(export_payload["separate_knowledge_state_included"])
            self.assertEqual(
                export_payload["separate_knowledge_command"],
                "noruct knowledge export DESTINATION --state STATE_DB",
            )
            self.assertEqual(support_code, EXIT_OK, error.getvalue())
            self.assertEqual(denied, EXIT_INPUT)
            self.assertIn("requires --confirm", error.getvalue())
            self.assertEqual(deleted, EXIT_OK, error.getvalue())
            deletion_payload = json.loads(deleted_output.getvalue())
            self.assertTrue(deletion_payload["deleted"])
            self.assertEqual(deletion_payload["data_scope"], "runtime-company-state")
            self.assertFalse(deletion_payload["separate_knowledge_state_deleted"])
            self.assertEqual(
                deletion_payload["separate_knowledge_command"],
                "noruct knowledge delete --state STATE_DB --confirm",
            )
            self.assertFalse(state.exists())
            self.assertTrue(knowledge_database.exists())
            self.assertTrue(knowledge_vault.exists())
            self.assertTrue(exported.exists())
            self.assertTrue(support.exists())

    def test_data_help_names_the_separate_knowledge_lifecycle(self) -> None:
        rendered = io.StringIO()
        with contextlib.redirect_stdout(rendered), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["data", "--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = rendered.getvalue()
        self.assertIn("runtime/company SQLite state only", help_text)
        self.assertIn("noruct knowledge", " ".join(help_text.split()))

        for action in ("export", "delete"):
            rendered = io.StringIO()
            with contextlib.redirect_stdout(rendered), self.assertRaises(SystemExit) as raised:
                build_parser().parse_args(["data", action, "--help"])
            self.assertEqual(raised.exception.code, 0)
            normalized = " ".join(rendered.getvalue().split())
            self.assertIn("runtime/company SQLite state only", normalized)
            self.assertIn(f"noruct knowledge {action}", normalized)


if __name__ == "__main__":
    unittest.main()
