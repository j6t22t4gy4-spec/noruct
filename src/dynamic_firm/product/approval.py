from __future__ import annotations

import asyncio

from dynamic_firm.runtime.models import ApprovalDecision, ApprovalRequest, ToolEffect
from dynamic_firm.runtime.ports import CancellationToken

from .tui import InlineTerminalUI


class InteractiveApprovalController:
    """Serialize parallel employee approvals and cache only workspace-edit consent."""

    def __init__(self, ui: InlineTerminalUI) -> None:
        self.ui = ui
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._workspace_edits_allowed = False

    @staticmethod
    def _valid_decision(value: object) -> ApprovalDecision | None:
        return value if isinstance(value, ApprovalDecision) else None

    async def request(
        self,
        request: ApprovalRequest,
        cancellation: CancellationToken,
    ) -> ApprovalDecision:
        cancellation.raise_if_cancelled()
        if (
            self._workspace_edits_allowed
            and request.allow_session
            and request.effect == ToolEffect.WRITE
        ):
            return ApprovalDecision.ALLOW_SESSION
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        async with self._lock:
            cancellation.raise_if_cancelled()
            # A redraw/input race must not silently turn into DENY.  Retry the
            # same immutable request once; ToolExecutor records a distinct
            # UNAVAILABLE outcome if the terminal remains unusable.
            decision: ApprovalDecision | None = None
            for _attempt in range(2):
                try:
                    candidate = await asyncio.to_thread(self.ui.ask_approval, request)
                except Exception:
                    cancellation.raise_if_cancelled()
                    continue
                decision = self._valid_decision(candidate)
                if decision is not None:
                    break
            if decision is None:
                return ApprovalDecision.UNAVAILABLE
            cancellation.raise_if_cancelled()
            if (
                decision == ApprovalDecision.ALLOW_SESSION
                and request.allow_session
                and request.effect == ToolEffect.WRITE
            ):
                self._workspace_edits_allowed = True
            return decision
