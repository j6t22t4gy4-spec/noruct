"""Compatibility exports for the decomposed Employee tool surfaces."""

from .tool_contracts import (
    ApprovalPreview,
    CapabilityProjectionAudit,
    PolicyDenied,
    ResourceKey,
    ToolDefinition,
    ToolEffectNotStarted,
    ToolExecutionError,
    ToolRegistry,
    ToolValidationError,
    Validator,
    capability_projection,
)
from .tool_executor import ToolExecutor
from .models import IdempotencyMode, ToolRisk
from .workspace_read_tools import FixtureReader, WorkspaceReadTools
from .workspace_mutation_tools import (
    WorkspaceTools,
    atomic_write_text,
    checked_workspace_mutation_target,
    validate_workspace_mutation_path,
)

__all__ = (
    "ApprovalPreview",
    "CapabilityProjectionAudit",
    "FixtureReader",
    "IdempotencyMode",
    "PolicyDenied",
    "ResourceKey",
    "ToolDefinition",
    "ToolEffectNotStarted",
    "ToolExecutionError",
    "ToolExecutor",
    "ToolRegistry",
    "ToolRisk",
    "ToolValidationError",
    "Validator",
    "WorkspaceReadTools",
    "WorkspaceTools",
    "atomic_write_text",
    "capability_projection",
    "checked_workspace_mutation_target",
    "validate_workspace_mutation_path",
)
