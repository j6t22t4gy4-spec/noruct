from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

from dynamic_firm.knowledge.intake import (
    DoclingExtractor,
    ExtractedDocument,
    KnowledgeIntakeService,
    LocalDocumentExtractor,
)
from dynamic_firm.knowledge.models import AssetStatus, ProcessingStatus
from dynamic_firm.knowledge.store import KnowledgeStore
from dynamic_firm.knowledge.vault import KnowledgeVault


class _RevisionExtractor:
    name = "fixture-extractor"
    version = "2"

    def __init__(self) -> None:
        self.calls = 0

    def supports(self, source: Path, media_type: str) -> bool:
        del source, media_type
        return True

    def extract(
        self,
        source: Path,
        *,
        media_type: str,
        timeout_seconds: float,
    ) -> ExtractedDocument:
        del source, media_type, timeout_seconds
        self.calls += 1
        return ExtractedDocument(
            markdown=f"Revision {self.calls}: the launch codename is Cedar.",
            processor=self.name,
            processor_version=self.version,
        )


class KnowledgeIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = KnowledgeStore(self.root / "knowledge.db")
        self.vault = KnowledgeVault(self.root / "vault")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_plaintext_is_normalized_chunked_and_stored_with_private_permissions(self) -> None:
        source = self.root / "notes.txt"
        source.write_text("First line.\r\n\r\nSecond searchable line.\r\n", encoding="utf-8")

        result = KnowledgeIntakeService(self.store, self.vault).ingest(source)

        self.assertEqual(result.processing_status, ProcessingStatus.READY)
        self.assertEqual(result.asset.status, AssetStatus.READY)
        self.assertIsNotNone(result.representation)
        assert result.representation is not None
        self.assertEqual(stat.S_IMODE(self.vault.root.stat().st_mode), 0o700)
        stored = self.vault.resolve(result.asset.vault_relative_path)
        derived = self.vault.resolve(result.representation.vault_relative_path)
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(derived.stat().st_mode), 0o600)
        self.assertEqual(
            self.vault.read_text(result.representation.vault_relative_path),
            "First line.\n\nSecond searchable line.",
        )
        rows = self.store.retrieval_rows(access_scope="private")
        self.assertEqual("".join(str(row["content"]) for row in rows), "First line.\n\nSecond searchable line.")

    def test_unsupported_asset_is_preserved_and_never_reported_ready(self) -> None:
        source = self.root / "opaque.unknown"
        payload = b"\x00\x01\x02not-a-supported-document"
        source.write_bytes(payload)

        result = KnowledgeIntakeService(self.store, self.vault).ingest(source)

        self.assertEqual(result.processing_status, ProcessingStatus.STORED_UNPROCESSED)
        self.assertEqual(result.asset.status, AssetStatus.STORED_UNPROCESSED)
        self.assertIsNone(result.representation)
        self.assertIn("Original preserved", " ".join(result.messages))
        self.assertEqual(self.vault.resolve(result.asset.vault_relative_path).read_bytes(), payload)
        self.assertEqual(self.store.retrieval_rows(access_scope="private"), ())

    def test_source_symlink_directory_and_oversize_are_rejected(self) -> None:
        actual = self.root / "actual.txt"
        actual.write_text("safe", encoding="utf-8")
        link = self.root / "linked.txt"
        os.symlink(actual, link)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            self.vault.inspect_source(link)
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.vault.inspect_source(self.root)

        oversized = self.root / "oversized.bin"
        oversized.write_bytes(b"12345")
        with self.assertRaisesRegex(ValueError, "4 byte intake limit"):
            self.vault.inspect_source(oversized, max_bytes=4)

    def test_explicit_plain_text_selector_uses_the_builtin_extractor(self) -> None:
        source = self.root / "explicit.txt"
        source.write_text("Explicit selector needle.", encoding="utf-8")

        result = KnowledgeIntakeService(self.store, self.vault).ingest(
            source, processor="plain-text"
        )

        self.assertEqual(result.processing_status, ProcessingStatus.READY)
        self.assertIsNotNone(result.representation)
        self.assertEqual(result.asset.processor, "noruct-plain-text")

    def test_processing_rejects_a_tampered_vault_source(self) -> None:
        source = self.root / "tamper.txt"
        source.write_text("Original immutable text.", encoding="utf-8")
        intake = KnowledgeIntakeService(self.store, self.vault)
        first = intake.ingest(source)
        stored = self.vault.resolve(first.asset.vault_relative_path)
        stored.write_text("TAMPERED private text!", encoding="utf-8")

        result = intake.process(first.asset.asset_id, processor="plain-text")

        self.assertEqual(result.processing_status, ProcessingStatus.FAILED)
        self.assertIn("integrity", result.asset.processing_error.lower())
        self.assertEqual(len(self.store.list_representations(first.asset.asset_id)), 1)
        self.assertEqual(self.store.retrieval_rows(access_scope="private"), ())

    def test_failed_asset_create_rolls_back_a_new_unreferenced_vault_object(self) -> None:
        source = self.root / "orphan.txt"
        source.write_text("ORPHAN-SECRET-42", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Parent Knowledge Asset was not found"):
            KnowledgeIntakeService(self.store, self.vault).ingest(
                source, parent_asset_id="asset-missing"
            )

        self.assertEqual(self.store.list_assets(), ())
        self.assertEqual(
            [path for path in self.vault.root.rglob("*") if path.is_file()],
            [],
        )

    def test_failed_representation_commit_rolls_back_new_derived_object(self) -> None:
        source = self.root / "derived-failure.txt"
        source.write_text("Derived rollback needle.", encoding="utf-8")
        intake = KnowledgeIntakeService(self.store, self.vault)

        with mock.patch.object(
            self.store,
            "create_representation",
            side_effect=sqlite3.OperationalError("fixture database failure"),
        ):
            result = intake.ingest(source)

        self.assertEqual(result.processing_status, ProcessingStatus.FAILED)
        self.assertEqual(self.store.list_representations(result.asset.asset_id), ())
        derived_root = self.vault.root / "derived"
        derived_files = (
            [path for path in derived_root.rglob("*") if path.is_file()]
            if derived_root.exists()
            else []
        )
        self.assertEqual(derived_files, [])

    def test_duplicate_content_with_a_different_extension_reuses_one_asset(self) -> None:
        text = "One immutable piece of knowledge."
        first_path = self.root / "memo.txt"
        second_path = self.root / "memo.md"
        first_path.write_text(text, encoding="utf-8")
        second_path.write_text(text, encoding="utf-8")
        intake = KnowledgeIntakeService(self.store, self.vault)

        first = intake.ingest(first_path, title="First title")
        second = intake.ingest(second_path, title="Conflicting duplicate title")

        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.asset.asset_id, first.asset.asset_id)
        self.assertEqual(second.asset.original_name, "memo.txt")
        self.assertEqual(second.asset.title, "First title")
        self.assertEqual(self.store.counts()["knowledge_assets"], 1)
        self.assertEqual(self.store.counts()["knowledge_representations"], 1)

    def test_same_content_in_two_scopes_has_distinct_assets_and_vault_objects(self) -> None:
        source = self.root / "scoped.txt"
        source.write_text("Scope-specific knowledge", encoding="utf-8")
        intake = KnowledgeIntakeService(self.store, self.vault)

        private = intake.ingest(source, access_scope="private")
        team = intake.ingest(source, access_scope="team:alpha")

        self.assertNotEqual(private.asset.asset_id, team.asset.asset_id)
        self.assertNotEqual(private.asset.vault_relative_path, team.asset.vault_relative_path)
        self.assertEqual(
            {row["asset_id"] for row in self.store.retrieval_rows(access_scope="private")},
            {private.asset.asset_id},
        )
        self.assertEqual(
            {row["asset_id"] for row in self.store.retrieval_rows(access_scope="team:alpha")},
            {team.asset.asset_id},
        )

    def test_reprocessing_creates_a_new_immutable_representation_revision(self) -> None:
        source = self.root / "source.fixture"
        source.write_text("opaque source", encoding="utf-8")
        extractor = _RevisionExtractor()
        intake = KnowledgeIntakeService(self.store, self.vault, extractors=(extractor,))

        first = intake.ingest(source)
        second = intake.process(first.asset.asset_id)

        self.assertEqual(first.processing_status, ProcessingStatus.READY)
        self.assertEqual(second.processing_status, ProcessingStatus.READY)
        assert first.representation is not None and second.representation is not None
        self.assertEqual((first.representation.revision, second.representation.revision), (1, 2))
        self.assertNotEqual(
            first.representation.representation_id, second.representation.representation_id
        )
        self.assertNotEqual(
            first.representation.vault_relative_path, second.representation.vault_relative_path
        )
        rows = self.store.retrieval_rows(access_scope="private")
        self.assertEqual(
            {row["representation_id"] for row in rows},
            {second.representation.representation_id},
        )

    def test_docling_support_survives_content_addressed_storage_without_an_extension(self) -> None:
        extractor = DoclingExtractor()
        content_addressed_path = self.vault.root / "objects" / "aa" / ("f" * 64)
        self.assertTrue(extractor.supports(content_addressed_path, "application/pdf"))
        self.assertTrue(
            extractor.supports(
                content_addressed_path,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        )

    def test_builtin_local_worker_extracts_docx_without_an_external_dependency(self) -> None:
        source = self.root / "strategy.docx"
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\" />",
            )
            archive.writestr(
                "word/document.xml",
                """<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">
<w:body><w:p><w:r><w:t>Local DOCX evidence.</w:t></w:r></w:p>
<w:p><w:r><w:t>Second searchable paragraph.</w:t></w:r></w:p></w:body></w:document>""",
            )

        result = KnowledgeIntakeService(self.store, self.vault).ingest(source)

        self.assertEqual(result.processing_status, ProcessingStatus.READY)
        self.assertEqual(result.asset.processor, "local-document")
        self.assertIsNotNone(result.representation)
        assert result.representation is not None
        self.assertIn("docx-stdlib", result.representation.processor_version)
        self.assertEqual(
            self.vault.read_text(result.representation.vault_relative_path),
            "Local DOCX evidence.\n\nSecond searchable paragraph.",
        )

    def test_builtin_local_worker_extracts_pptx_without_docling(self) -> None:
        source = self.root / "strategy.pptx"
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr(
                "ppt/slides/slide1.xml",
                """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>
<p:sp><p:txBody><a:p><a:r><a:t>Launch strategy</a:t></a:r></a:p>
<a:p><a:r><a:t>North star evidence</a:t></a:r></a:p></p:txBody></p:sp>
</p:spTree></p:cSld></p:sld>""",
            )

        result = KnowledgeIntakeService(self.store, self.vault).ingest(source)

        self.assertEqual(result.processing_status, ProcessingStatus.READY)
        assert result.representation is not None
        self.assertIn("pptx-stdlib", result.representation.processor_version)
        rendered = self.vault.read_text(result.representation.vault_relative_path)
        self.assertIn("Launch strategy", rendered)
        self.assertIn("North star evidence", rendered)

    def test_builtin_local_worker_extracts_xlsx_shared_strings_without_docling(self) -> None:
        source = self.root / "metrics.xlsx"
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr(
                "xl/sharedStrings.xml",
                """<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<si><t>Metric</t></si><si><t>Qualified revenue</t></si></sst>""",
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
<row r="2"><c r="A2" t="inlineStr"><is><t>2026 Q3</t></is></c><c r="B2"><v>125000</v></c></row>
</sheetData></worksheet>""",
            )

        result = KnowledgeIntakeService(self.store, self.vault).ingest(source)

        self.assertEqual(result.processing_status, ProcessingStatus.READY)
        assert result.representation is not None
        self.assertIn("xlsx-stdlib", result.representation.processor_version)
        rendered = self.vault.read_text(result.representation.vault_relative_path)
        self.assertIn("Metric | Qualified revenue", rendered)
        self.assertIn("2026 Q3 | 125000", rendered)

    def test_builtin_local_worker_extracts_digital_pdf(self) -> None:
        source = self.root / "evidence.pdf"
        stream = b"BT /F1 12 Tf 72 720 Td (Local PDF evidence.) Tj ET"
        objects = (
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        )
        payload = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for ordinal, value in enumerate(objects, 1):
            offsets.append(len(payload))
            payload.extend(f"{ordinal} 0 obj\n".encode("ascii"))
            payload.extend(value)
            payload.extend(b"\nendobj\n")
        xref = len(payload)
        payload.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
        payload.extend(b"".join(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:]))
        payload.extend(
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
        )
        source.write_bytes(payload)

        result = KnowledgeIntakeService(self.store, self.vault).ingest(source)

        self.assertEqual(result.processing_status, ProcessingStatus.READY)
        self.assertIsNotNone(result.representation)
        assert result.representation is not None
        self.assertIn("Local PDF evidence.", self.vault.read_text(result.representation.vault_relative_path))

    def test_builtin_local_worker_uses_local_image_metadata_without_network_or_ocr(self) -> None:
        def chunk(kind: bytes, data: bytes) -> bytes:
            return (
                len(data).to_bytes(4, "big")
                + kind
                + data
                + (zlib.crc32(kind + data) & 0xFFFFFFFF).to_bytes(4, "big")
            )

        source = self.root / "caption.png"
        png = b"\x89PNG\r\n\x1a\n" + b"".join(
            (
                chunk(b"IHDR", (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"),
                chunk(b"tEXt", b"Description\x00Local image metadata evidence."),
                chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")),
                chunk(b"IEND", b""),
            )
        )
        source.write_bytes(png)

        result = KnowledgeIntakeService(self.store, self.vault).ingest(source)

        self.assertEqual(result.processing_status, ProcessingStatus.READY)
        self.assertIsNotNone(result.representation)
        assert result.representation is not None
        self.assertIn(
            "Local image metadata evidence.",
            self.vault.read_text(result.representation.vault_relative_path),
        )

    def test_local_document_selector_is_available_for_supported_content_addressed_files(self) -> None:
        extractor = LocalDocumentExtractor()
        content_addressed_path = self.vault.root / "objects" / "aa" / ("f" * 64)
        self.assertTrue(extractor.supports(content_addressed_path, "application/pdf"))
        self.assertTrue(extractor.supports(content_addressed_path, "image/png"))
        self.assertTrue(
            extractor.supports(
                content_addressed_path,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        )
        self.assertTrue(
            extractor.supports(
                content_addressed_path,
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
        )
        self.assertTrue(
            extractor.supports(
                content_addressed_path,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        )

    def test_exact_pinned_offline_worker_contract_processes_pdf_end_to_end(self) -> None:
        worker = self.root / "docling-worker"
        worker.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

request = json.loads(sys.stdin.read())
if request["network_policy"] != "DENY":
    raise SystemExit(10)
if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("NO_PROXY") != "*":
    raise SystemExit(11)
if os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
    raise SystemExit(12)
print(json.dumps({
    "schema_version": "noruct.docling-worker-response.v1",
    "source_sha256": request["source_sha256"],
    "source_commit": "9b51f4f857176cdd95cef53e2ec7f5f32ffbc6a5",
    "worker_sha256": request["worker_sha256"],
    "remote_io_performed": False,
    "status": "SUCCESS",
    "markdown": "# Converted offline\\n\\nNeedle PDF evidence.",
}))
""",
            encoding="utf-8",
        )
        worker.chmod(0o700)
        manifest = self.root / "worker-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "noruct.docling-worker-manifest.v1",
                    "source_commit": "9b51f4f857176cdd95cef53e2ec7f5f32ffbc6a5",
                    "offline_required": True,
                    "remote_io_allowed": False,
                    "trust_model": "operator-trusted-local-executable",
                    "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
                    "source_wheel_sha256": "da5b91e7c5c4028459c533596a4d8f1579212000673b564751c66e1daae0ddbd",
                    "dependency_lock_sha256": "b" * 64,
                    "asset_manifest_sha256": "c" * 64,
                    "profiles": ["standard"],
                    "worker_version": "fixture-1",
                }
            ),
            encoding="utf-8",
        )
        extractor = DoclingExtractor(worker, manifest)
        source = self.root / "paper.pdf"
        source.write_bytes(b"%PDF-1.4 fixture")

        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "must-not-cross", "ANTHROPIC_API_KEY": "must-not-cross"},
        ):
            result = KnowledgeIntakeService(
                self.store, self.vault, extractors=(extractor,)
            ).ingest(source)

        self.assertTrue(extractor.available)
        self.assertIn("fixture-1+9b51f4f85717", extractor.version)
        self.assertEqual(result.processing_status, ProcessingStatus.READY)
        self.assertIsNotNone(result.representation)
        assert result.representation is not None
        self.assertEqual(
            self.vault.read_text(result.representation.vault_relative_path),
            "# Converted offline\n\nNeedle PDF evidence.",
        )

    def test_worker_manifest_that_allows_remote_io_is_not_admitted(self) -> None:
        worker = self.root / "docling-worker"
        worker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        worker.chmod(0o700)
        manifest = self.root / "worker-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "noruct.docling-worker-manifest.v1",
                    "source_commit": "9b51f4f857176cdd95cef53e2ec7f5f32ffbc6a5",
                    "offline_required": True,
                    "remote_io_allowed": True,
                    "trust_model": "operator-trusted-local-executable",
                    "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
                    "source_wheel_sha256": "da5b91e7c5c4028459c533596a4d8f1579212000673b564751c66e1daae0ddbd",
                    "dependency_lock_sha256": "b" * 64,
                    "asset_manifest_sha256": "c" * 64,
                    "profiles": ["standard"],
                }
            ),
            encoding="utf-8",
        )

        extractor = DoclingExtractor(worker, manifest)

        self.assertFalse(extractor.available)
        self.assertEqual(extractor.version, "unavailable")

    def test_worker_executable_must_match_manifest_and_tampering_is_rejected(self) -> None:
        worker = self.root / "docling-worker"
        worker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        worker.chmod(0o700)
        manifest = self.root / "worker-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "noruct.docling-worker-manifest.v1",
                    "source_commit": "9b51f4f857176cdd95cef53e2ec7f5f32ffbc6a5",
                    "offline_required": True,
                    "remote_io_allowed": False,
                    "trust_model": "operator-trusted-local-executable",
                    "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
                    "source_wheel_sha256": "da5b91e7c5c4028459c533596a4d8f1579212000673b564751c66e1daae0ddbd",
                    "dependency_lock_sha256": "b" * 64,
                    "asset_manifest_sha256": "c" * 64,
                    "profiles": ["standard"],
                }
            ),
            encoding="utf-8",
        )
        extractor = DoclingExtractor(worker, manifest)
        self.assertTrue(extractor.available)

        worker.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        worker.chmod(0o700)

        self.assertFalse(extractor.available)
        self.assertEqual(extractor.version, "unavailable")


if __name__ == "__main__":
    unittest.main()
