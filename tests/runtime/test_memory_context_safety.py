from __future__ import annotations

import unittest

from dynamic_firm._vendor.runtime_safety.memory_context import StreamingContextScrubber, sanitize_context


class MemoryContextSafetyTests(unittest.TestCase):
    def test_split_fenced_context_is_discarded_without_hiding_visible_answer(self) -> None:
        scrubber = StreamingContextScrubber()
        parts = (
            scrubber.feed("Before\n<memory-con"),
            scrubber.feed("text>\nsecret-value\n</memory-context>\nAfter"),
            scrubber.flush(),
        )

        self.assertEqual("".join(parts), "Before\n\nAfter")
        self.assertEqual(sanitize_context("<memory-context>x</memory-context>Visible"), "Visible")


if __name__ == "__main__":
    unittest.main()
