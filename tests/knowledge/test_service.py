from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from dynamic_firm.knowledge.service import UserKnowledgeService
from dynamic_firm.knowledge.remote_fetch import (
    conditional_download_public_https_asset,
    RemoteKnowledgeDownload,
    RemoteKnowledgeFetchError,
    RemoteKnowledgeNotModified,
    validate_public_https_url,
)
from dynamic_firm.knowledge.store import KnowledgeStore
from dynamic_firm.knowledge.vault import KnowledgeVault, VaultObject


class UserKnowledgeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = KnowledgeStore(self.root / "knowledge.db")
        self.vault = KnowledgeVault(self.root / "vault")
        self.service = UserKnowledgeService(self.store, self.vault)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _ingest(
        self,
        name: str,
        content: str,
        *,
        scope: str = "private",
    ):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return self.service.ingest(path, access_scope=scope)

    def _stage_asset_delete(self, asset_id: str):
        assets = self.store.asset_deletion_closure(asset_id)
        representations = tuple(
            representation
            for item in assets
            for representation in self.store.list_representations(item.asset_id)
        )
        journal = self.vault.begin_delete(
            asset_id=asset_id,
            expected_asset_ids=tuple(item.asset_id for item in assets),
            expected_representation_ids=tuple(
                item.representation_id for item in representations
            ),
            objects=(
                *(
                    VaultObject(
                        item.content_hash,
                        item.byte_size,
                        item.vault_relative_path,
                    )
                    for item in assets
                ),
                *(
                    VaultObject(
                        item.content_hash,
                        item.byte_size,
                        item.vault_relative_path,
                    )
                    for item in representations
                ),
            ),
        )
        return self.vault.stage_journal_delete(journal), assets, representations

    def test_irrelevant_query_produces_an_explicit_empty_evidence_pack(self) -> None:
        self._ingest("memo.txt", "The release codename is Cedar.")

        pack = self.service.build_evidence_pack("volcano zebra", persist=False)

        self.assertEqual(pack.items, ())
        self.assertEqual(pack.selected_bytes, 0)
        self.assertEqual(pack.candidate_count, 1)
        pack.verify()
        self.assertIn("No matching evidence was selected", pack.runtime_projection())

    def test_explicit_public_https_import_is_copied_locally_then_transport_file_is_removed(self) -> None:
        temporary_download = self.root / "remote-download.txt"
        temporary_download.write_text("Remote pricing evidence.", encoding="utf-8")
        download = RemoteKnowledgeDownload(
            "https://example.com/pricing.txt", "text/plain", temporary_download.stat().st_size, temporary_download
        )
        with mock.patch(
            "dynamic_firm.knowledge.service.download_public_https_asset",
            return_value=download,
        ):
            result, receipt = self.service.ingest_public_https(
                download.source_url, title="Remote pricing"
            )
        self.assertEqual(receipt.source_url, download.source_url)
        self.assertEqual(result.asset.origin, download.source_url)
        self.assertFalse(temporary_download.exists())
        self.assertTrue((self.vault.root / result.asset.vault_relative_path).is_file())
        source = self.store.remote_asset_source(result.asset.asset_id)
        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual(source["source_url"], download.source_url)

    def test_explicit_public_https_import_can_require_one_exact_download_digest(self) -> None:
        temporary_download = self.root / "remote-digest.txt"
        body = b"Pinned remote pricing evidence."
        temporary_download.write_bytes(body)
        download = RemoteKnowledgeDownload(
            "https://example.com/pricing.txt", "text/plain", len(body), temporary_download
        )
        with mock.patch(
            "dynamic_firm.knowledge.service.download_public_https_asset",
            return_value=download,
        ):
            result, _receipt = self.service.ingest_public_https(
                download.source_url,
                expected_sha256=hashlib.sha256(body).hexdigest(),
            )

        self.assertEqual(result.asset.origin, download.source_url)
        self.assertFalse(temporary_download.exists())

    def test_remote_digest_mismatch_rejects_before_intake_and_removes_transport_file(self) -> None:
        temporary_download = self.root / "remote-bad-digest.txt"
        temporary_download.write_text("Unexpected response.", encoding="utf-8")
        download = RemoteKnowledgeDownload(
            "https://example.com/pricing.txt", "text/plain", temporary_download.stat().st_size, temporary_download
        )
        with mock.patch(
            "dynamic_firm.knowledge.service.download_public_https_asset",
            return_value=download,
        ):
            with self.assertRaises(RemoteKnowledgeFetchError):
                self.service.ingest_public_https(
                    download.source_url,
                    expected_sha256="0" * 64,
                )

        self.assertFalse(temporary_download.exists())
        self.assertEqual(self.store.list_assets(), ())

    def test_invalid_expected_digest_is_rejected_before_any_remote_read(self) -> None:
        with mock.patch(
            "dynamic_firm.knowledge.service.download_public_https_asset"
        ) as download:
            with self.assertRaises(RemoteKnowledgeFetchError):
                self.service.ingest_public_https(
                    "https://example.com/pricing.txt",
                    expected_sha256="not-a-digest",
                )

        download.assert_not_called()

    def test_explicit_remote_refresh_creates_a_parent_linked_asset_only_for_changed_content(self) -> None:
        original = self._ingest("prior.txt", "Prior public evidence.")
        source_url = "https://example.com/pricing.txt"
        self.store.record_remote_asset_source(
            original.asset.asset_id,
            source_url=source_url,
            etag='"prior"',
            last_modified=None,
            content_fetched=True,
        )
        downloaded_path = self.root / "refreshed.txt"
        downloaded_path.write_text("Updated public evidence.", encoding="utf-8")
        download = RemoteKnowledgeDownload(
            source_url, "text/plain", downloaded_path.stat().st_size, downloaded_path,
            etag='"updated"',
        )
        with mock.patch(
            "dynamic_firm.knowledge.service.conditional_download_public_https_asset",
            return_value=download,
        ):
            outcome = self.service.refresh_public_https(original.asset.asset_id)

        self.assertEqual(outcome["status"], "UPDATED")
        replacement = outcome["result"].asset
        self.assertEqual(replacement.parent_asset_id, original.asset.asset_id)
        self.assertFalse(downloaded_path.exists())
        source = self.store.remote_asset_source(replacement.asset_id)
        self.assertEqual(source["etag"], '"updated"')

    def test_remote_refresh_digest_mismatch_preserves_prior_asset(self) -> None:
        original = self._ingest("prior.txt", "Prior public evidence.")
        source_url = "https://example.com/pricing.txt"
        self.store.record_remote_asset_source(
            original.asset.asset_id,
            source_url=source_url,
            etag='"prior"',
            last_modified=None,
            content_fetched=True,
        )
        downloaded_path = self.root / "wrong-refresh.txt"
        downloaded_path.write_text("Unexpected refresh.", encoding="utf-8")
        download = RemoteKnowledgeDownload(
            source_url, "text/plain", downloaded_path.stat().st_size, downloaded_path, etag='"next"'
        )
        with mock.patch(
            "dynamic_firm.knowledge.service.conditional_download_public_https_asset",
            return_value=download,
        ):
            with self.assertRaises(RemoteKnowledgeFetchError):
                self.service.refresh_public_https(
                    original.asset.asset_id,
                    expected_sha256="f" * 64,
                )

        self.assertFalse(downloaded_path.exists())
        self.assertEqual(len(self.store.list_assets()), 1)

    def test_explicit_remote_refresh_304_preserves_the_existing_asset(self) -> None:
        original = self._ingest("prior.txt", "Prior public evidence.")
        source_url = "https://example.com/pricing.txt"
        self.store.record_remote_asset_source(
            original.asset.asset_id,
            source_url=source_url,
            etag='"prior"',
            last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
            content_fetched=True,
        )
        with mock.patch(
            "dynamic_firm.knowledge.service.conditional_download_public_https_asset",
            return_value=RemoteKnowledgeNotModified(source_url, etag='"prior"'),
        ):
            outcome = self.service.refresh_public_https(original.asset.asset_id)

        self.assertEqual(outcome["status"], "NOT_MODIFIED")
        self.assertEqual(outcome["asset"].asset_id, original.asset.asset_id)
        self.assertEqual(len(self.store.list_assets()), 1)

    def test_remote_fetch_rejects_non_public_or_non_https_sources_before_network(self) -> None:
        for source in (
            "http://example.com/a.txt",
            "https://localhost/a.txt",
            "https://127.0.0.1/a.txt",
            "https://192.168.1.1/a.txt",
            "https://user:password@example.com/a.txt",
        ):
            with self.assertRaises(RemoteKnowledgeFetchError):
                validate_public_https_url(source)

    def test_conditional_remote_refresh_sends_bounded_validators_and_maps_304(self) -> None:
        headers = Message()
        headers["ETag"] = '"current"'
        headers["Last-Modified"] = "Wed, 01 Jan 2025 00:00:00 GMT"
        failure = HTTPError(
            "https://example.com/brief.txt", 304, "Not Modified", headers, None
        )
        opener = mock.Mock()
        opener.open.side_effect = failure
        with mock.patch(
            "dynamic_firm.knowledge.remote_fetch.build_opener", return_value=opener
        ):
            result = conditional_download_public_https_asset(
                "https://example.com/brief.txt",
                if_none_match='"prior"',
                if_modified_since="Tue, 31 Dec 2024 00:00:00 GMT",
            )

        self.assertIsInstance(result, RemoteKnowledgeNotModified)
        assert isinstance(result, RemoteKnowledgeNotModified)
        self.assertEqual(result.etag, '"current"')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("If-none-match"), '"prior"')
        self.assertEqual(
            request.get_header("If-modified-since"), "Tue, 31 Dec 2024 00:00:00 GMT"
        )

    def test_evidence_selection_uses_the_full_documented_candidate_ceiling(self) -> None:
        self._ingest("candidate-bound.txt", "Needle evidence remains eligible.")

        with mock.patch.object(self.store, "retrieval_rows", wraps=self.store.retrieval_rows) as rows:
            pack = self.service.build_evidence_pack("needle", persist=False)

        self.assertEqual(len(pack.items), 1)
        self.assertEqual(rows.call_args.kwargs["limit"], 10_000)

    def test_managed_chunk_fts_projection_is_optional_scoped_and_deleted_with_asset(self) -> None:
        result = self._ingest(
            "pricing-brief.txt",
            "Pricing strategy evidence for managed-target-007.",
        )
        if not self.store.managed_fts5_available():
            self.assertIsNone(
                self.store.indexed_representation_retrieval_rows(
                    access_scope="private", query="pricing strategy managed-target-007"
                )
            )
            return

        indexed = self.store.indexed_representation_retrieval_rows(
            access_scope="private", query="pricing strategy managed-target-007"
        )
        assert indexed is not None
        self.assertEqual(len(indexed), 1)
        self.assertEqual(indexed[0]["asset_id"], result.asset.asset_id)
        pack = self.service.build_evidence_pack(
            "pricing strategy managed-target-007", persist=False
        )
        self.assertEqual(pack.candidate_count, 1)
        self.assertEqual(pack.items[0].asset_id, result.asset.asset_id)

        self.service.delete_asset(result.asset.asset_id)
        self.assertIsNone(
            self.store.indexed_representation_retrieval_rows(
                access_scope="private", query="pricing strategy managed-target-007"
            )
        )

    def test_managed_cjk_candidate_projection_handles_connected_compound_surface(self) -> None:
        result = self._ingest(
            "pricing-change-ko.txt",
            "가격 전략 변경을 위한 근거를 검토합니다.",
        )
        self.assertTrue(self.store.managed_cjk_candidate_index_available())

        indexed = self.store.indexed_representation_retrieval_rows(
            access_scope="private",
            query="가격전략의변경을",
        )

        assert indexed is not None
        self.assertEqual([row["asset_id"] for row in indexed], [result.asset.asset_id])
        self.assertIn("가격 전략 변경", str(indexed[0]["content"]))

    def test_evidence_pack_enforces_total_and_per_excerpt_bounds(self) -> None:
        content = "needle " + ("x" * 180)
        self._ingest("bounded.txt", content)

        pack = self.service.build_evidence_pack(
            "needle",
            limit=1,
            max_bytes=256,
            max_excerpt_bytes=128,
            persist=False,
        )

        self.assertEqual(len(pack.items), 1)
        self.assertLessEqual(pack.selected_bytes, 256)
        self.assertLessEqual(len(pack.items[0].excerpt.encode("utf-8")), 128)
        pack.verify(maximum_bytes=256, maximum_items=1)

        oversized_item = dataclasses.replace(
            pack.items[0],
            title="T" * 500_000,
            location={"blob": "L" * 500_000},
        )
        oversized = dataclasses.replace(pack, items=(oversized_item,), digest="")
        oversized = dataclasses.replace(oversized, digest=oversized.computed_digest())
        with self.assertRaisesRegex(ValueError, "metadata|payload"):
            oversized.verify(maximum_bytes=256, maximum_items=1)

        with self.assertRaisesRegex(ValueError, "kind"):
            self.store.create_record(
                kind="K" * 500_000,
                statement="needle",
            )
        with self.assertRaisesRegex(ValueError, "source span"):
            self.store.create_record(
                kind="NOTE",
                statement="needle",
                source_span={"blob": "L" * 500_000},
            )

    def test_evidence_pack_keeps_a_relevant_long_chunk_with_a_bounded_excerpt(self) -> None:
        self._ingest("long.txt", "needle " + "x" * 8_000)

        pack = self.service.build_evidence_pack(
            "needle",
            limit=1,
            max_bytes=512,
            max_excerpt_bytes=512,
            persist=False,
        )

        self.assertEqual(len(pack.items), 1)
        self.assertEqual(pack.selected_bytes, 512)
        self.assertTrue(pack.items[0].excerpt.startswith("needle"))
        pack.verify(maximum_bytes=512, maximum_items=1)

    def test_evidence_pack_object_and_persisted_payload_fail_closed_on_tamper(self) -> None:
        self._ingest("evidence.txt", "Needle evidence with an immutable citation.")
        pack = self.service.build_evidence_pack("needle")
        self.assertEqual(len(pack.items), 1)
        self.assertEqual(pack.schema_version, "noruct.evidence-pack.v3")
        restored = self.store.evidence_pack(pack.pack_id)
        assert restored is not None
        self.assertEqual(restored.items[0].retrieval_basis, pack.items[0].retrieval_basis)

        changed_item = dataclasses.replace(pack.items[0], excerpt="tampered")
        with self.assertRaises(ValueError):
            dataclasses.replace(pack, items=(changed_item,)).verify()

        connection = sqlite3.connect(self.store.path)
        try:
            payload_text = connection.execute(
                "SELECT payload_json FROM evidence_packs WHERE pack_id = ?", (pack.pack_id,)
            ).fetchone()[0]
            payload = json.loads(payload_text)
            payload["items"][0]["excerpt"] = "tampered in sqlite"
            connection.execute(
                "UPDATE evidence_packs SET payload_json = ? WHERE pack_id = ?",
                (json.dumps(payload, sort_keys=True), pack.pack_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(ValueError):
            self.store.evidence_pack(pack.pack_id)

    def test_scope_isolation_survives_deleting_the_same_content_from_one_scope(self) -> None:
        source = self.root / "shared.txt"
        source.write_text("Scoped needle evidence", encoding="utf-8")
        private = self.service.ingest(source, access_scope="private")
        team = self.service.ingest(source, access_scope="team:alpha")
        private_pack = self.service.build_evidence_pack("needle", access_scope="private")
        team_pack = self.service.build_evidence_pack("needle", access_scope="team:alpha")
        private_path = self.vault.resolve(private.asset.vault_relative_path)
        team_path = self.vault.resolve(team.asset.vault_relative_path)

        self.service.delete_asset(private.asset.asset_id)

        self.assertFalse(private_path.exists())
        self.assertTrue(team_path.exists())
        self.assertIsNone(self.store.asset(private.asset.asset_id))
        self.assertIsNotNone(self.store.asset(team.asset.asset_id))
        self.assertIsNone(self.store.evidence_pack(private_pack.pack_id))
        remaining = self.store.evidence_pack(team_pack.pack_id)
        self.assertIsNotNone(remaining)
        assert remaining is not None
        remaining.verify()
        self.assertEqual({item.asset_id for item in remaining.items}, {team.asset.asset_id})

    def test_scope_cannot_be_laundered_through_record_or_pack_provenance(self) -> None:
        team = self._ingest(
            "team-secret.txt",
            "TEAM-SCOPE-SECRET-71b2 needle",
            scope="team:alpha",
        )
        assert team.representation is not None
        with self.assertRaisesRegex(ValueError, "same scope"):
            self.store.create_record(
                kind="NOTE",
                statement="Copy the team secret into private scope.",
                source_asset_id=team.asset.asset_id,
                source_representation_id=team.representation.representation_id,
                access_scope="private",
            )

        team_record = self.store.create_record(
            kind="NOTE",
            statement="Team-only conclusion.",
            source_asset_id=team.asset.asset_id,
            source_representation_id=team.representation.representation_id,
            access_scope="team:alpha",
        )
        with self.assertRaisesRegex(ValueError, "cannot cross"):
            self.store.create_record(
                kind="NOTE",
                statement="Private correction that launders a team record.",
                supersedes_record_id=team_record.record_id,
                access_scope="private",
            )

        team_pack = self.service.build_evidence_pack(
            "needle", access_scope="team:alpha", persist=False
        )
        private_pack = dataclasses.replace(team_pack, access_scope="private", digest="")
        private_pack = dataclasses.replace(
            private_pack, digest=private_pack.computed_digest()
        )
        with self.assertRaisesRegex(ValueError, "scope or provenance"):
            self.store.save_evidence_pack(private_pack)

        private = self.service.build_evidence_pack(
            "TEAM-SCOPE-SECRET-71b2", access_scope="private", persist=False
        )
        self.assertEqual(private.items, ())

    def test_asset_delete_is_atomic_propagates_derived_rows_and_preserves_sentinel(self) -> None:
        result = self._ingest("delete-me.txt", "Needle source that will be deleted.")
        assert result.representation is not None
        record = self.store.create_record(
            kind="claim",
            statement="Needle claim derived from the source.",
            source_asset_id=result.asset.asset_id,
            source_representation_id=result.representation.representation_id,
        )
        pack = self.service.build_evidence_pack("needle")
        source_path = self.vault.resolve(result.asset.vault_relative_path)
        representation_path = self.vault.resolve(result.representation.vault_relative_path)
        sentinel = self.vault.root / "unrelated-sentinel.txt"
        sentinel.write_text("preserve", encoding="utf-8")

        with mock.patch.object(self.store, "delete_asset", side_effect=RuntimeError("db failure")):
            with self.assertRaisesRegex(RuntimeError, "db failure"):
                self.service.delete_asset(result.asset.asset_id)
        self.assertTrue(source_path.exists())
        self.assertTrue(representation_path.exists())
        self.assertTrue(sentinel.exists())

        self.service.delete_asset(result.asset.asset_id)

        self.assertFalse(source_path.exists())
        self.assertFalse(representation_path.exists())
        self.assertTrue(sentinel.exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
        self.assertIsNone(self.store.asset(result.asset.asset_id))
        self.assertIsNone(self.store.record(record.record_id))
        self.assertIsNone(self.store.evidence_pack(pack.pack_id))
        self.assertEqual(self.store.integrity_check(), "ok")

    def test_asset_delete_crash_before_db_commit_restores_staged_objects(self) -> None:
        result = self._ingest("crash-before.txt", "Restore me after a staged deletion crash.")
        assert result.representation is not None
        source = self.vault.resolve(result.asset.vault_relative_path)
        derived = self.vault.resolve(result.representation.vault_relative_path)

        journal, _, _ = self._stage_asset_delete(result.asset.asset_id)
        self.assertEqual(journal.phase, "staged")
        self.assertFalse(source.exists())
        self.assertFalse(derived.exists())
        self.assertTrue(self.vault.delete_journal_path.exists())

        recovered = UserKnowledgeService(self.store, self.vault)

        self.assertIsNotNone(recovered)
        self.assertTrue(source.is_file())
        self.assertTrue(derived.is_file())
        self.assertFalse(self.vault.delete_journal_path.exists())
        self.assertIsNotNone(self.store.asset(result.asset.asset_id))

    def test_asset_delete_crash_after_db_commit_finalizes_staged_objects(self) -> None:
        result = self._ingest("crash-after.txt", "Finalize me after a DB commit crash.")
        assert result.representation is not None
        source = self.vault.resolve(result.asset.vault_relative_path)
        derived = self.vault.resolve(result.representation.vault_relative_path)
        journal, assets, representations = self._stage_asset_delete(result.asset.asset_id)
        self.store.delete_asset(
            result.asset.asset_id,
            expected_asset_ids={item.asset_id for item in assets},
            expected_representation_ids={
                item.representation_id for item in representations
            },
        )

        self.assertTrue(self.vault.delete_journal_path.exists())
        UserKnowledgeService(self.store, self.vault)

        self.assertFalse(source.exists())
        self.assertFalse(derived.exists())
        self.assertFalse(self.vault.delete_journal_path.exists())
        self.assertIsNone(self.store.asset(result.asset.asset_id))

    def test_asset_delete_recovery_preserves_a_new_db_reference_to_reused_content(self) -> None:
        source_file = self.root / "reused-after-crash.txt"
        source_file.write_text("Content-addressed source reused after commit.", encoding="utf-8")
        result = self.service.ingest(source_file)
        assert result.representation is not None
        source_path = self.vault.resolve(result.asset.vault_relative_path)
        old_derived = self.vault.resolve(result.representation.vault_relative_path)
        _, assets, representations = self._stage_asset_delete(result.asset.asset_id)
        self.store.delete_asset(
            result.asset.asset_id,
            expected_asset_ids={item.asset_id for item in assets},
            expected_representation_ids={
                item.representation_id for item in representations
            },
        )

        receipt = self.vault.store_source(
            source_file,
            content_hash=result.asset.content_hash,
            byte_size=result.asset.byte_size,
            access_scope=result.asset.access_scope,
        )
        replacement, duplicate = self.store.create_asset(
            content_hash=result.asset.content_hash,
            original_name="replacement.txt",
            title="Replacement reference",
            media_type="text/plain",
            byte_size=result.asset.byte_size,
            vault_relative_path=receipt.relative_path,
            origin="test-concurrent-reuse",
            access_scope=result.asset.access_scope,
        )
        self.assertFalse(duplicate)

        recovered = UserKnowledgeService(self.store, self.vault)

        self.assertEqual(recovered.last_recovery, "FINALIZED_WITH_REUSED_OBJECTS")
        self.assertTrue(source_path.is_file())
        self.assertFalse(old_derived.exists())
        self.assertIsNotNone(self.store.asset(replacement.asset_id))
        self.assertFalse(self.vault.delete_journal_path.exists())

    def test_asset_delete_rejects_a_concurrent_representation_and_rolls_back(self) -> None:
        result = self._ingest("race.txt", "Original representation remains recoverable.")
        assert result.representation is not None
        source = self.vault.resolve(result.asset.vault_relative_path)
        original_derived = self.vault.resolve(result.representation.vault_relative_path)
        original_delete = self.store.delete_asset

        def raced_delete(asset_id: str, **options):
            content = "Concurrent representation"
            receipt = self.vault.write_representation(asset_id, content)
            self.store.create_representation(
                asset_id=asset_id,
                kind="concurrent_markdown",
                media_type="text/markdown",
                content_hash=receipt.content_hash,
                byte_size=receipt.byte_size,
                vault_relative_path=receipt.relative_path,
                processor="test-race",
                processor_version="1",
                chunks=(
                    {
                        "content": content,
                        "content_hash": receipt.content_hash,
                        "char_start": 0,
                        "char_end": len(content),
                        "location": {},
                    },
                ),
            )
            return original_delete(asset_id, **options)

        with mock.patch.object(self.store, "delete_asset", side_effect=raced_delete):
            with self.assertRaisesRegex(ValueError, "representations changed"):
                self.service.delete_asset(result.asset.asset_id)

        self.assertTrue(source.is_file())
        self.assertTrue(original_derived.is_file())
        self.assertFalse(self.vault.delete_journal_path.exists())
        self.assertEqual(len(self.store.list_representations(result.asset.asset_id)), 2)

    def test_vault_rejects_symlink_root_and_inner_path_components(self) -> None:
        actual = self.root / "actual-vault"
        actual.mkdir()
        linked_root = self.root / "linked-vault"
        linked_root.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "root must not be a symbolic link"):
            KnowledgeVault(linked_root)

        inner_target = self.vault.root / "inner-target"
        inner_target.mkdir()
        inner_link = self.vault.root / "inner-link"
        inner_link.symlink_to(inner_target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "contains a symbolic link"):
            self.vault.resolve("inner-link/object")

    def test_deleting_a_parent_asset_removes_its_revision_subtree_and_vault_files(self) -> None:
        parent = self._ingest("parent.txt", "Parent revision secret needle.")
        child_path = self.root / "child.txt"
        child_path.write_text("Child revision secret needle.", encoding="utf-8")
        child = self.service.ingest(
            child_path,
            parent_asset_id=parent.asset.asset_id,
        )
        parent_vault = self.vault.resolve(parent.asset.vault_relative_path)
        child_vault = self.vault.resolve(child.asset.vault_relative_path)

        self.service.delete_asset(parent.asset.asset_id)

        self.assertIsNone(self.store.asset(parent.asset.asset_id))
        self.assertIsNone(self.store.asset(child.asset.asset_id))
        self.assertFalse(parent_vault.exists())
        self.assertFalse(child_vault.exists())

    def test_forgetting_a_record_deletes_packs_that_cited_it_but_not_other_records(self) -> None:
        original = self.store.create_record(kind="decision", statement="Choose Cedar.")
        corrected = self.store.create_record(
            kind="decision",
            statement="Choose Birch instead.",
            supersedes_record_id=original.record_id,
        )
        pack = self.service.build_evidence_pack("Birch")
        self.assertEqual({item.source_id for item in pack.items}, {corrected.record_id})

        self.assertTrue(self.store.forget_record(corrected.record_id))

        self.assertIsNone(self.store.evidence_pack(pack.pack_id))
        previous = self.store.record(original.record_id)
        self.assertIsNotNone(previous)
        assert previous is not None
        self.assertEqual(previous.status, "SUPERSEDED")

    def test_write_candidate_requires_explicit_accept_and_preserves_provenance_scope(self) -> None:
        self._ingest("team.txt", "Team needle evidence.", scope="team:alpha")
        pack = self.service.build_evidence_pack("needle", access_scope="team:alpha")

        candidate = self.store.create_write_candidate(
            job_id="job-123",
            kind="DECISION",
            statement="Use the team evidence.",
            evidence_pack_id=pack.pack_id,
        )
        replayed_creation = self.store.create_write_candidate(
            job_id="job-123",
            kind="DECISION",
            statement="Use the team evidence.",
            evidence_pack_id=pack.pack_id,
        )
        self.assertEqual(replayed_creation.candidate_id, candidate.candidate_id)
        self.assertEqual(self.store.list_records(), ())

        accepted = self.store.resolve_write_candidate(candidate.candidate_id, accept=True)
        self.assertEqual(accepted.status, "ACCEPTED")
        self.assertIsNotNone(accepted.accepted_record_id)
        assert accepted.accepted_record_id is not None
        record = self.store.record(accepted.accepted_record_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.source_candidate_id, candidate.candidate_id)
        self.assertEqual(record.source_job_id, "job-123")
        self.assertEqual(record.evidence_pack_id, pack.pack_id)

        private_ids = {
            row["source_id"] for row in self.store.retrieval_rows(access_scope="private")
        }
        team_ids = {
            row["source_id"] for row in self.store.retrieval_rows(access_scope="team:alpha")
        }
        self.assertNotIn(record.record_id, private_ids)
        self.assertIn(record.record_id, team_ids)

    def test_asset_forget_removes_transitive_candidate_record_pack_and_decision_content(self) -> None:
        result = self._ingest("secret.txt", "SOURCE-SECRET-6d5c9e evidence needle")
        pack = self.service.build_evidence_pack("needle")
        candidate = self.store.create_write_candidate(
            job_id="job-secret",
            statement="RESULT-SECRET-1fa82e derived from the source",
            evidence_pack_id=pack.pack_id,
        )
        accepted = self.store.resolve_write_candidate(candidate.candidate_id, accept=True)
        assert accepted.accepted_record_id is not None
        decision = self.store.create_decision(
            statement="DECISION-SECRET-d38a11",
            rationale="Derived from the private evidence",
            evidence_pack_id=pack.pack_id,
        )
        downstream_pack = self.service.build_evidence_pack("derived")
        self.assertEqual(
            {item.source_id for item in downstream_pack.items},
            {accepted.accepted_record_id},
        )
        downstream = self.store.create_write_candidate(
            job_id="job-secret-downstream",
            statement="DOWNSTREAM-SECRET-ea590b",
            evidence_pack_id=downstream_pack.pack_id,
        )
        downstream_accepted = self.store.resolve_write_candidate(
            downstream.candidate_id, accept=True
        )

        self.service.delete_asset(result.asset.asset_id)

        self.assertIsNone(self.store.asset(result.asset.asset_id))
        self.assertIsNone(self.store.evidence_pack(pack.pack_id))
        self.assertIsNone(self.store.evidence_pack(downstream_pack.pack_id))
        self.assertIsNone(self.store.write_candidate(candidate.candidate_id))
        self.assertIsNone(self.store.write_candidate(downstream.candidate_id))
        self.assertIsNone(self.store.record(accepted.accepted_record_id))
        assert downstream_accepted.accepted_record_id is not None
        self.assertIsNone(self.store.record(downstream_accepted.accepted_record_id))
        self.assertIsNone(self.store.decision(decision.decision_id))
        remaining = "\n".join(
            str(row["content"]) for row in self.store.retrieval_rows(access_scope="private")
        )
        self.assertNotIn("SECRET", remaining)

    def test_write_candidate_resolution_replay_is_idempotent(self) -> None:
        candidate = self.store.create_write_candidate(
            job_id="job-retry",
            kind="NOTE",
            statement="Preserve exactly one result across a retry.",
        )
        accepted = self.store.resolve_write_candidate(candidate.candidate_id, accept=True)

        replayed_accept = self.store.resolve_write_candidate(candidate.candidate_id, accept=True)

        self.assertEqual(replayed_accept, accepted)
        self.assertEqual(self.store.counts()["knowledge_records"], 1)
        with self.assertRaisesRegex(ValueError, "already resolved"):
            self.store.resolve_write_candidate(candidate.candidate_id, accept=False)

    def test_rejected_write_candidate_never_creates_a_knowledge_record(self) -> None:
        candidate = self.store.create_write_candidate(
            job_id="job-rejected",
            kind="NOTE",
            statement="This result should not enter the Knowledge DB.",
        )

        rejected = self.store.resolve_write_candidate(candidate.candidate_id, accept=False)

        self.assertEqual(rejected.status, "REJECTED")
        self.assertIsNone(rejected.accepted_record_id)
        self.assertEqual(self.store.list_records(), ())


if __name__ == "__main__":
    unittest.main()
