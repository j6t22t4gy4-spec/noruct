from __future__ import annotations

import json
import io
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_firm.company.models import canonical_json
from dynamic_firm.product.executable_plugins import ExecutablePlugin, ExecutablePluginStore
from dynamic_firm.product.plugin_catalog import (
    CATALOG_SCHEMA,
    PluginCatalogError,
    PluginCatalog,
    PluginCatalogEntry,
    PluginCatalogSource,
    PluginCatalogStore,
    parse_catalog,
)
from dynamic_firm.cli import EXIT_OK, main


class PluginCatalogTests(unittest.TestCase):
    def _payload(self) -> bytes:
        return canonical_json(
            {
                "schema": CATALOG_SCHEMA,
                "catalog_id": "official",
                "entries": [
                    {
                        "plugin_id": "echo",
                        "version": "1.0.0",
                        "description": "Echo a bounded value.",
                        "repository_url": "https://github.com/example/echo.git",
                        "commit": "a" * 40,
                        "subdirectory": ".",
                    }
                ],
            }
        ).encode("utf-8")

    def test_catalog_requires_canonical_signed_exact_commit_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            allowed = Path(temp) / "allowed_signers"; allowed.write_text("official ssh-ed25519 AAAA\n", encoding="utf-8")
            with patch("dynamic_firm.product.plugin_catalog.verify_openssh_signature_bytes", return_value={"principal": "official", "payload_digest": "digest"}) as verify:
                catalog = parse_catalog(
                    self._payload(), source_url="https://catalog.example/catalog.json", signature_url="https://catalog.example/catalog.sig",
                    signature=b"signature", allowed_signers_path=allowed, principal="official", ssh_keygen=Path("/usr/bin/ssh-keygen"),
                )
            self.assertEqual(catalog.catalog_id, "official")
            self.assertEqual(catalog.entries[0].commit, "a" * 40)
            self.assertEqual(verify.call_args.kwargs["namespace"], "noruct-executable-plugin-catalog-v1")

    def test_catalog_rejects_noncanonical_or_branch_entries_before_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            allowed = Path(temp) / "allowed_signers"; allowed.write_text("official ssh-ed25519 AAAA\n", encoding="utf-8")
            noncanonical = json.dumps(json.loads(self._payload()), indent=2).encode("utf-8")
            with self.assertRaisesRegex(PluginCatalogError, "canonical"):
                parse_catalog(noncanonical, source_url="https://catalog.example/catalog.json", signature_url="https://catalog.example/catalog.sig", signature=b"signature", allowed_signers_path=allowed, principal="official", ssh_keygen=Path("/usr/bin/ssh-keygen"))
            invalid = json.loads(self._payload()); invalid["entries"][0]["commit"] = "main"
            with self.assertRaisesRegex(PluginCatalogError, "commit"):
                parse_catalog(canonical_json(invalid).encode("utf-8"), source_url="https://catalog.example/catalog.json", signature_url="https://catalog.example/catalog.sig", signature=b"signature", allowed_signers_path=allowed, principal="official", ssh_keygen=Path("/usr/bin/ssh-keygen"))

    def test_staged_catalog_installs_only_its_exact_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugins"; allowed = Path(temp) / "allowed_signers"; allowed.write_text("official ssh-ed25519 AAAA\n", encoding="utf-8")
            store = PluginCatalogStore(root)
            with patch("dynamic_firm.product.plugin_catalog._bounded_fetch", side_effect=[self._payload(), b"signature"]), patch("dynamic_firm.product.plugin_catalog.verify_openssh_signature_bytes", return_value={"principal": "official"}):
                staged = store.fetch_and_stage(source_url="https://catalog.example/catalog.json", signature_url="https://catalog.example/catalog.sig", allowed_signers_path=allowed, principal="official", ssh_keygen=Path("/usr/bin/ssh-keygen"))
            self.assertEqual(store.list()[0].digest, staged.digest)
            plugin_store = ExecutablePluginStore(root)
            with patch.object(plugin_store, "install_git", return_value="installed") as install:
                self.assertEqual(store.install("official", "echo", version="1.0.0", plugin_store=plugin_store), "installed")
            install.assert_called_once_with(
                "https://github.com/example/echo.git", "a" * 40, subdirectory=".",
                catalog_provenance={"catalog_id": "official", "catalog_digest": staged.digest},
            )

    def test_candidates_are_local_receipt_comparison_not_an_automatic_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugins"; allowed = Path(temp) / "allowed_signers"; allowed.write_text("official ssh-ed25519 AAAA\n", encoding="utf-8")
            store = PluginCatalogStore(root)
            with patch("dynamic_firm.product.plugin_catalog._bounded_fetch", side_effect=[self._payload(), b"signature"]), patch("dynamic_firm.product.plugin_catalog.verify_openssh_signature_bytes", return_value={"principal": "official"}):
                store.fetch_and_stage(source_url="https://catalog.example/catalog.json", signature_url="https://catalog.example/catalog.sig", allowed_signers_path=allowed, principal="official", ssh_keygen=Path("/usr/bin/ssh-keygen"))
            self.assertEqual(len(store.candidates(())), 1)
            installed = ExecutablePlugin(
                plugin_id="echo", version="1.0.0", description="installed", package_path=root, command=("host",),
                environment_names=(), timeout_seconds=5.0, tools=(), package_digest="a" * 64, enabled=False,
            )
            self.assertEqual(store.candidates((installed,)), ())

    def test_cli_candidate_projection_never_contacts_a_catalog_or_installs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugins"; config = Path(temp) / "config.toml"; allowed = Path(temp) / "allowed_signers"; allowed.write_text("official ssh-ed25519 AAAA\n", encoding="utf-8")
            store = PluginCatalogStore(root)
            with patch("dynamic_firm.product.plugin_catalog._bounded_fetch", side_effect=[self._payload(), b"signature"]), patch("dynamic_firm.product.plugin_catalog.verify_openssh_signature_bytes", return_value={"principal": "official"}):
                store.fetch_and_stage(source_url="https://catalog.example/catalog.json", signature_url="https://catalog.example/catalog.sig", allowed_signers_path=allowed, principal="official", ssh_keygen=Path("/usr/bin/ssh-keygen"))
            output = io.StringIO()
            code = main(["--config", str(config), "plugin", "catalog-candidates", "--root", str(root), "--json"], stdout=output, stderr=io.StringIO())
        payload = json.loads(output.getvalue())
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(payload["network_attempted"])
        self.assertEqual(payload["candidate_count"], 1)
        self.assertIn("catalog-install official echo --version 1.0.0 --catalog-digest", payload["candidates"][0]["next_action"])

    def test_latest_snapshot_controls_candidates_and_install_not_digest_sort_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugins"; store = PluginCatalogStore(root)
            def catalog(version: str, commit: str, verified_at: str) -> PluginCatalog:
                entry = PluginCatalogEntry("echo", version, "Echo.", "https://github.com/example/echo.git", commit, ".")
                raw = canonical_json({"schema": CATALOG_SCHEMA, "catalog_id": "official", "entries": [{"plugin_id": entry.plugin_id, "version": entry.version, "description": entry.description, "repository_url": entry.repository_url, "commit": entry.commit, "subdirectory": entry.subdirectory}]}).encode("utf-8")
                return PluginCatalog("official", hashlib.sha256(raw).hexdigest(), (entry,), "https://catalog.example/catalog.json", "https://catalog.example/catalog.sig", verified_at, {"principal": "official"})
            older = catalog("1.0.0", "a" * 40, "2026-07-24T01:00:00+00:00")
            newer = catalog("2.0.0", "b" * 40, "2026-07-24T02:00:00+00:00")
            with patch("dynamic_firm.product.plugin_catalog._bounded_fetch", side_effect=[b"one", b"one-signature", b"two", b"two-signature"]), patch("dynamic_firm.product.plugin_catalog.parse_catalog", side_effect=[older, newer]):
                store.fetch_and_stage(source_url=older.source_url, signature_url=older.signature_url, allowed_signers_path=Path("/tmp/signers"), principal="official", ssh_keygen=Path("/usr/bin/ssh-keygen"))
                store.fetch_and_stage(source_url=newer.source_url, signature_url=newer.signature_url, allowed_signers_path=Path("/tmp/signers"), principal="official", ssh_keygen=Path("/usr/bin/ssh-keygen"))
            self.assertEqual(store.latest()[0].digest, newer.digest)
            self.assertEqual(store.candidates(())[0].candidate_version, "2.0.0")
            plugin_store = ExecutablePluginStore(root)
            with patch.object(plugin_store, "install_git", return_value="installed") as install:
                self.assertEqual(store.install("official", "echo", version=None, catalog_digest=newer.digest, plugin_store=plugin_store), "installed")
            install.assert_called_once_with(
                "https://github.com/example/echo.git", "b" * 40, subdirectory=".",
                catalog_provenance={"catalog_id": "official", "catalog_digest": newer.digest},
            )
            with self.assertRaisesRegex(PluginCatalogError, "Multiple catalog snapshots"):
                store.install("official", "echo", version=None, plugin_store=plugin_store)

    def test_registered_catalog_source_refreshes_only_its_expected_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugins"; allowed = Path(temp) / "allowed_signers"; allowed.write_text("official ssh-ed25519 AAAA\n", encoding="utf-8")
            store = PluginCatalogStore(root)
            source = store.register_source(PluginCatalogSource("official", "https://catalog.example/catalog.json", "https://catalog.example/catalog.sig", allowed, "official", Path(sys.executable).resolve()))
            self.assertEqual(store.list_sources(), (source,))
            staged = PluginCatalog("official", "a" * 64, (), source.source_url, source.signature_url, "2026-07-24T01:00:00+00:00", {"principal": "official"})
            with patch.object(store, "fetch_and_stage", return_value=staged) as fetch:
                self.assertEqual(store.refresh_source("official"), staged)
            self.assertEqual(fetch.call_args.kwargs["expected_catalog_id"], "official")
            self.assertTrue(store.remove_source("official"))
            self.assertEqual(store.list_sources(), ())

    def test_cli_catalog_source_registration_is_local_until_explicit_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "plugins"; config = Path(temp) / "config.toml"; allowed = Path(temp) / "allowed_signers"; allowed.write_text("official ssh-ed25519 AAAA\n", encoding="utf-8")
            output = io.StringIO()
            code = main([
                "--config", str(config), "plugin", "catalog-source-add", "--catalog-id", "official",
                "--url", "https://catalog.example/catalog.json", "--signature-url", "https://catalog.example/catalog.sig",
                "--allowed-signers", str(allowed), "--principal", "official", "--ssh-keygen", str(Path(sys.executable).resolve()),
                "--root", str(root), "--confirm", "--json",
            ], stdout=output, stderr=io.StringIO())
            payload = json.loads(output.getvalue()); output.seek(0); output.truncate(0)
            listed = main(["--config", str(config), "plugin", "catalog-source-list", "--root", str(root), "--json"], stdout=output, stderr=io.StringIO())
        self.assertEqual(code, EXIT_OK); self.assertFalse(payload["network_attempted"])
        self.assertEqual(listed, EXIT_OK); self.assertEqual(json.loads(output.getvalue())["source_count"], 1)
