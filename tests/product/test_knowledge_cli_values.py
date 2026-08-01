from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dynamic_firm.knowledge import KnowledgeStore
from dynamic_firm.product.knowledge_cli_values import knowledge_limit, show_knowledge_value


class KnowledgeCliValueTests(unittest.TestCase):
    def test_limit_is_bounded_before_a_store_query(self) -> None:
        self.assertEqual(knowledge_limit(1), 1)
        self.assertEqual(knowledge_limit(500, label="asset list limit"), 500)
        with self.assertRaisesRegex(ValueError, "between 1 and 500"):
            knowledge_limit(0)
        with self.assertRaisesRegex(ValueError, "between 1 and 500"):
            knowledge_limit(501)

    def test_show_only_accepts_known_typed_identifier_families(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = KnowledgeStore(Path(temporary) / "knowledge.db")
            try:
                record = store.create_record(kind="NOTE", statement="typed lookup")
                payload = show_knowledge_value(store, record.record_id)
                self.assertEqual(payload.record_id, record.record_id)
                with self.assertRaisesRegex(ValueError, "prefix"):
                    show_knowledge_value(store, "sqlite_master")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
