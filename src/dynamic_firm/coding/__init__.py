"""First-party shadow coding contracts and execution services."""

from typing import Any

from .constants import APPLY_CHANGE_SET_TOOL
from .models import (
    CodingExecutionProgress,
    CodingExecutionProgressKind,
    CodingWorkRequest,
    CodingWorkResult,
    ValidationAttempt,
    FileChangeKind,
    ShadowCodingOutcome,
    WorkspaceChangeSet,
    WorkspaceFileChange,
)
from .ports import CodingValidatorPort, CodingWorkerError, CodingWorkerPort
__all__ = [
    "APPLY_CHANGE_SET_TOOL",
    "ChangeSetCatalog",
    "CodingExecutionProgress",
    "CodingExecutionProgressKind",
    "CodingValidatorPort",
    "CodingWorkRequest",
    "CodingWorkResult",
    "ValidationAttempt",
    "CodingWorkerError",
    "CodingWorkerPort",
    "FileChangeKind",
    "RoutedEmployeeExecutionService",
    "ShadowCodingEmployeeRuntimeService",
    "ShadowCodingOutcome",
    "ShadowWorkspaceError",
    "ShadowWorkspaceLimits",
    "ShadowWorkspaceService",
    "WorkspaceChangeSet",
    "WorkspaceFileChange",
]


_LAZY_SERVICE_EXPORTS = frozenset(
    {
        "ChangeSetCatalog",
        "RoutedEmployeeExecutionService",
        "ShadowCodingEmployeeRuntimeService",
        "ShadowWorkspaceError",
        "ShadowWorkspaceLimits",
        "ShadowWorkspaceService",
    }
)


def __getattr__(name: str) -> Any:
    """Load runtime services only for callers that request them.

    The change-set identifier remains direct for Company learning while the
    runtime store is initializing.  The shadow workspace and services own
    runtime-tool dependencies, so they are resolved only when requested.
    """

    if name in _LAZY_SERVICE_EXPORTS:
        from . import service

        if hasattr(service, name):
            return getattr(service, name)
        from . import shadow

        return getattr(shadow, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
