"""Policy-neutral selection between native and shadow Employee execution."""

from __future__ import annotations

from collections.abc import AsyncIterator

from dynamic_firm.runtime.models import (
    CancelReceipt,
    EmployeeRunRequest,
    EmployeeRunResult,
    RunEvent,
    RunHandle,
)
from dynamic_firm.runtime.ports import EmployeeExecutionPort

from .shadow import APPLY_CHANGE_SET_TOOL


class RoutedEmployeeExecutionService:
    """Select direct host tools by default and shadow workspaces when warranted.

    A change-set grant makes the shadow worker *available*; it must not make
    every implementation request pay the cost of snapshotting the workspace.
    The native employee already owns the audited file and terminal tool
    contracts, including approval checkpoints. Small operational tasks stay
    there, while explicitly broad coding work can use a disposable worktree.
    """

    _BROAD_CODING_MARKERS = (
        "refactor", "architecture", "repository-wide", "multiple files",
        "multi-file", "migration", "large-scale", "리팩터", "구조 개편",
        "전체 구조", "전반적인", "여러 파일", "다수 파일", "대규모", "마이그레이션",
    )

    def __init__(
        self,
        *,
        native: EmployeeExecutionPort,
        shadow_coding: EmployeeExecutionPort,
        host_direct_only: bool = False,
    ) -> None:
        self.native = native
        self.shadow_coding = shadow_coding
        self.host_direct_only = host_direct_only
        self._routes: dict[str, EmployeeExecutionPort] = {}

    def _select(self, request: EmployeeRunRequest) -> str:
        shadow_available = any(
            grant.tool_name == APPLY_CHANGE_SET_TOOL
            for grant in request.action_policy.tool_grants
        )
        if (
            shadow_available
            and not self.host_direct_only
            and request.action_policy.sandbox_profile == "shadow-workspace-approved"
        ):
            return "shadow"
        return (
            "shadow"
            if self.should_use_shadow(
                request.task.objective,
                shadow_available=shadow_available,
                host_direct_only=self.host_direct_only,
            )
            else "native"
        )

    @classmethod
    def should_use_shadow(
        cls,
        objective: str,
        *,
        shadow_available: bool,
        host_direct_only: bool,
    ) -> bool:
        """Return whether a broad coding request merits an isolated workspace."""

        if not shadow_available or host_direct_only:
            return False
        normalized = objective.casefold()
        return any(marker.casefold() in normalized for marker in cls._BROAD_CODING_MARKERS)

    async def start(self, request: EmployeeRunRequest) -> RunHandle:
        service: EmployeeExecutionPort = (
            self.shadow_coding if self._select(request) == "shadow" else self.native
        )
        handle = await service.start(request)
        self._routes[handle.run_id] = service
        return handle

    def _service(self, handle: RunHandle) -> EmployeeExecutionPort:
        service = self._routes.get(handle.run_id)
        if service is None:
            raise KeyError(f"Unknown routed run handle: {handle.run_id}")
        return service

    def observe(self, handle: RunHandle, after_seq: int = 0) -> AsyncIterator[RunEvent]:
        return self._service(handle).observe(handle, after_seq)

    async def cancel(self, handle: RunHandle, reason: str) -> CancelReceipt:
        return await self._service(handle).cancel(handle, reason)

    async def collect(self, handle: RunHandle) -> EmployeeRunResult:
        return await self._service(handle).collect(handle)

    async def close(
        self,
        reason: str = "Runtime service shutdown",
        grace_seconds: float = 1.0,
    ) -> None:
        await self.shadow_coding.close(reason, grace_seconds)
        await self.native.close(reason, grace_seconds)
