"""Dependency-free operator-timeline bounds adapted from Paperclip.

Upstream: https://github.com/paperclipai/paperclip
Commit: ce7dedf33d2689673826ffdcfd6af7ee06be39af
Source file: server/src/services/work-timeline.ts
SHA-256: 7ac8fa0e407d915a223e3d45760c3eec471f6f7a5ae08d604fa7838331d5a45a
Upstream test: server/src/__tests__/work-timeline-service.test.ts
SHA-256: 8eb57f4c90e7067164eee383db64d6c24ab490933007a7f71c065e69223dc4c3
Copyright (c) 2025 Paperclip AI. SPDX-License-Identifier: MIT.

Modifications: ported from TypeScript to dependency-free Python and narrowed to
the two operator-query admission rules used by Noruct's first-party ACTIVE JOB
timeline. It neither queries a database nor exposes Paperclip actors, issues,
ACLs, state, routes, UI, assets, or product identity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


DEFAULT_EVENT_LIMIT = 200
MAX_EVENT_LIMIT = 500
MAX_WINDOW = timedelta(days=31)
DEFAULT_WINDOW = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class TimelineWindow:
    from_at: datetime
    to_at: datetime
    capped: bool


def normalize_event_limit(value: object) -> int:
    """Return the bounded count admitted to a read-only operator query."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_EVENT_LIMIT
    if not math.isfinite(value):
        return DEFAULT_EVENT_LIMIT
    return max(1, min(MAX_EVENT_LIMIT, math.floor(value)))


def normalize_timeline_window(
    *,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    now: datetime | None = None,
) -> TimelineWindow:
    """Bound a read-only timeline to a recent, non-future UTC interval."""

    current = _utc(now or datetime.now(timezone.utc))
    raw_to = _utc(to_at) if to_at is not None else current
    end = min(raw_to, current)
    start = _utc(from_at) if from_at is not None else end - DEFAULT_WINDOW
    capped = False
    if end - start > MAX_WINDOW:
        start = end - MAX_WINDOW
        capped = True
    if start > end:
        start = end - DEFAULT_WINDOW
        capped = True
    return TimelineWindow(from_at=start, to_at=end, capped=capped)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timeline timestamps must include a UTC offset")
    return value.astimezone(timezone.utc)
