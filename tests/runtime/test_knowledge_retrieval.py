from __future__ import annotations

from datetime import UTC, datetime
import unittest

from dynamic_firm.runtime.knowledge_retrieval import BoundedKnowledgeRetriever
from dynamic_firm.runtime.models import VersionedContent


class BoundedKnowledgeRetrieverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.retriever = BoundedKnowledgeRetriever()

    def test_cjk_bigrams_match_unspaced_document_text(self) -> None:
        selection = self.retriever.select(
            (
                VersionedContent("asset:pricing", "1", "가격전략은 현재 유지한다."),
                VersionedContent("asset:other", "1", "채용 계획을 검토한다."),
            ),
            query="가격 전략",
            limit=1,
        )

        self.assertEqual(selection.items[0].content_id, "asset:pricing")
        self.assertEqual(self.retriever.revision, "bounded-hybrid-cjk-v4")

    def test_filename_phrase_and_epistemic_signals_are_disclosed_separately(self) -> None:
        preferred = VersionedContent(
            "folder_file:pricing", "1", "Current strategy keeps the list price stable."
        )
        conflicted = VersionedContent(
            "folder_file:archive", "1", "Current strategy keeps the list price stable."
        )

        selection = self.retriever.select(
            (conflicted, preferred),
            query="pricing strategy",
            limit=1,
            metadata={
                conflicted.content_id: {
                    "title": "archive.md",
                    "path": "archive/notes.md",
                    "trust_class": "UNTRUSTED_EXTERNAL",
                    "conflict_refs": ("record:other",),
                    "freshness_expires_at": "2025-01-01T00:00:00+00:00",
                },
                preferred.content_id: {
                    "title": "pricing strategy.md",
                    "path": "strategy/pricing-strategy.md",
                    "trust_class": "TRUSTED_SOURCE",
                    "conflict_refs": (),
                    "freshness_expires_at": "2027-01-01T00:00:00+00:00",
                },
            },
            now=datetime(2026, 7, 27, tzinfo=UTC),
        )

        self.assertEqual(selection.items, (preferred,))
        explanation = selection.explanations[0]
        self.assertGreater(explanation.filename_path_score, 0)
        self.assertGreater(explanation.phrase_score, 0)
        self.assertGreater(explanation.trust_score, 0)
        self.assertIn("exact_phrase:title_or_path", explanation.basis)
        self.assertIn("freshness:current", explanation.basis)

    def test_partial_mode_reserves_only_available_excerpt_budget(self) -> None:
        candidate = VersionedContent("asset:long", "1", "needle " + "x" * 2_000)

        strict = self.retriever.select((candidate,), query="needle", limit=1, max_bytes=128)
        partial = self.retriever.select(
            (candidate,), query="needle", limit=1, max_bytes=128, allow_partial=True
        )

        self.assertEqual(strict.items, ())
        self.assertEqual(partial.items, (candidate,))
        self.assertEqual(partial.selected_bytes, 128)
