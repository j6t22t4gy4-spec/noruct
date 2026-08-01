from __future__ import annotations

import unittest

from dynamic_firm.runtime.context_compaction import BoundedContextCompactor
from dynamic_firm.runtime.models import ModelMessage


class ContextCompactionTests(unittest.TestCase):
    def test_preserves_head_recent_tool_group_and_uses_content_free_digest(self) -> None:
        messages = (
            ModelMessage("system", "policy"),
            ModelMessage("user", "current task"),
            ModelMessage("assistant", {"tool_calls": [{"call_id": "old"}]}),
            ModelMessage("tool", {"content": "old secret-like evidence"}, "old"),
            ModelMessage("assistant", {"tool_calls": [{"call_id": "recent"}]}),
            ModelMessage("tool", {"content": "recent evidence"}, "recent"),
            ModelMessage("user", {"runtime_error": "repair"}),
        )

        result = BoundedContextCompactor().compact(
            messages,
            max_messages=5,
            max_chars=10_000,
            keep_recent_messages=2,
        )

        self.assertTrue(result.compacted)
        self.assertEqual(result.messages[:2], messages[:2])
        self.assertNotIn("old secret-like evidence", repr(result.messages))
        self.assertIn("recent evidence", repr(result.messages))
        boundary = result.messages[2].content["runtime_context_compaction"]
        self.assertTrue(boundary["historical_only"])
        self.assertEqual(len(boundary["source_sha256"]), 64)
        self.assertEqual(result.removed_message_count, 2)

    def test_session_projection_preserves_structured_recent_tool_group(self) -> None:
        history = (
            {"role": "user", "content": "old request"},
            {"role": "assistant", "content": "old answer"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "recent", "function": {"name": "read"}}],
            },
            {"role": "tool", "tool_call_id": "recent", "content": "recent evidence"},
            {"role": "assistant", "content": "recent answer"},
        )

        result = BoundedContextCompactor().compact_session_history(
            history,
            max_messages=4,
            max_chars=10_000,
            keep_recent_messages=3,
        )

        self.assertTrue(result.compacted)
        self.assertNotIn("old request", repr(result.messages))
        self.assertIn("recent evidence", repr(result.messages))
        self.assertEqual(result.messages[-3]["tool_calls"][0]["id"], "recent")
        self.assertEqual(result.messages[-2]["tool_call_id"], "recent")
        boundary = result.messages[0]["content"]["runtime_context_compaction"]
        self.assertEqual(boundary["revision"], "noruct-head-tail-digest-v2")
        self.assertTrue(boundary["historical_only"])
        self.assertEqual(result.removed_message_count, 2)


if __name__ == "__main__":
    unittest.main()
