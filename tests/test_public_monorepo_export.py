from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from dev.export_public_monorepo import PublicExportError, export, selected_files
from dev.verify_public_monorepo import PublicMonorepoError, REQUIRED, verify


ROOT = Path(__file__).parents[1]


class PublicMonorepoExportTests(unittest.TestCase):
    def test_public_core_has_mit_license_and_package_metadata(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)["project"]
        self.assertEqual(project["license"], "MIT")
        self.assertEqual(project["license-files"], ["LICENSE"])
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", license_text)
        self.assertIn("Permission is hereby granted", license_text)

    def test_allow_list_cannot_be_widened_over_private_hosted_roots(self) -> None:
        manifest = {
            "include_exact": [],
            "include_prefixes": ["services/", "src/"],
            "private_exact": ["AGENTS.md"],
            "private_prefixes": ["services/evolution-network-worker/"],
        }
        files = selected_files(
            (
                "services/evolution-network-worker/src/index.ts",
                "src/dynamic_firm/foundation/runtime.py",
            ),
            manifest,
        )
        self.assertEqual(files, ("src/dynamic_firm/foundation/runtime.py",))

    def test_export_is_core_complete_and_hosted_service_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "public"
            receipt = export(project_root=ROOT, destination=target)
            verification = verify(target)
            self.assertFalse(receipt["hosted_service_included"])
            self.assertIsNone(receipt["public_document_commit"])
            self.assertTrue(verification["ok"])
            self.assertTrue((target / "src/dynamic_firm/kernel/service.py").is_file())
            self.assertFalse((target / "services/evolution-network-worker").exists())
            self.assertFalse((target / "AGENTS.md").exists())

    def test_export_refuses_nonempty_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "owned.txt").write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(PublicExportError, "empty directory"):
                export(project_root=ROOT, destination=target)

    def test_verifier_rejects_private_server_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            for relative in (
                *REQUIRED,
                "services/evolution-network-worker/src/index.ts",
            ):
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if relative == "public-monorepo.toml":
                    path.write_text('publication_state = "SOURCE_PUBLICATION_AUTHORIZED"\n', encoding="utf-8")
                else:
                    path.write_text("fixture\n", encoding="utf-8")
            with self.assertRaisesRegex(PublicMonorepoError, "private paths"):
                verify(target)


if __name__ == "__main__":
    unittest.main()
