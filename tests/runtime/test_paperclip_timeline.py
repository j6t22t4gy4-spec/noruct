from __future__ import annotations

from datetime import datetime, timezone
import unittest

from dynamic_firm._vendor.paperclip_runtime.timeline import (
    DEFAULT_EVENT_LIMIT,
    MAX_EVENT_LIMIT,
    normalize_event_limit,
    normalize_timeline_window,
)


class PaperclipTimelineBoundsTests(unittest.TestCase):
    def test_normalizes_event_limits_without_unbounded_operator_reads(self) -> None:
        self.assertEqual(normalize_event_limit(None), DEFAULT_EVENT_LIMIT)
        self.assertEqual(normalize_event_limit(True), DEFAULT_EVENT_LIMIT)
        self.assertEqual(normalize_event_limit(-4), 1)
        self.assertEqual(normalize_event_limit(12.9), 12)
        self.assertEqual(normalize_event_limit(9_999), MAX_EVENT_LIMIT)

    def test_caps_windows_and_refuses_naive_timestamps(self) -> None:
        now = datetime(2026, 3, 15, tzinfo=timezone.utc)
        window = normalize_timeline_window(
            from_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            to_at=now,
            now=now,
        )

        self.assertTrue(window.capped)
        self.assertEqual(window.from_at.isoformat(), "2026-02-12T00:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            normalize_timeline_window(from_at=datetime(2026, 1, 1), now=now)
