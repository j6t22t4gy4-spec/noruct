"""Dependency-free approval transition classifier adapted from Paperclip.

Upstream: https://github.com/paperclipai/paperclip
Commit: ce7dedf33d2689673826ffdcfd6af7ee06be39af
Source file:
- server/src/services/approvals.ts
  925f81e56ac6c43f71df29a0abe9b6006c5bb044c72a7a534e03ed96400ad950
Upstream tests:
- server/src/__tests__/approvals-service.test.ts
  ca8ce711a4be4c281710194f5e7e08d7f2882312a436a9aa3cc11bba09e49838
- server/src/__tests__/approval-routes-idempotency.test.ts
  8fb06d015b76f82789f75643360cb98f01199b51081ec848769d38fefb1a5ef8
Copyright (c) 2025 Paperclip AI. SPDX-License-Identifier: MIT.

Modifications: ported from TypeScript to dependency-free Python; narrowed
Paperclip's pending/revision/approved/rejected domain to Noruct's one-shot
tool decisions; database mutation, actor authority, events, and resume leases
remain first-party RunStore responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PrivateApprovalTransition:
    applied: bool
    conflict: bool


def classify_approval_transition(
    current_decision: str | None,
    target_decision: str,
) -> PrivateApprovalTransition:
    """Classify a decision retry after the database's compare-and-set step.

    A pending request may accept one target. Repeating that exact terminal
    decision is a successful no-op; trying to replace it is a conflict.
    """

    if current_decision is None:
        return PrivateApprovalTransition(applied=True, conflict=False)
    if current_decision == target_decision:
        return PrivateApprovalTransition(applied=False, conflict=False)
    return PrivateApprovalTransition(applied=False, conflict=True)
