"""Pure worker-boundary helpers for the private Employee Runtime.

These helpers own interpreter selection, the restricted worker code
projection, and child-selected tool-schema validation.  They have no RunStore,
provider, approval, or lifecycle authority; :mod:`runtime` remains the sole
service owner for those concerns.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm._vendor.runtime_safety.memory_context import sanitize_context
from dynamic_firm.runtime.models import ModelResponse, ToolSchema

from .protocol import FoundationProtocolError


_TOOL_DISCLOSURE_BRIDGE_NAMES = frozenset({"tool_search", "tool_describe", "tool_call"})
_WORKER_LOCAL_TOOL_NAMES = frozenset({"todo"})


def _default_worker_python() -> str:
    """Find the audited local worker interpreter without installing anything."""

    candidates = [os.environ.get("NORUCT_EMPLOYEE_RUNTIME_PYTHON", ""), sys.executable]
    candidates.extend(shutil.which(command) or "" for command in ("python3.11", "python3", "python"))
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        resolved = str(Path(candidate).expanduser().resolve())
        if resolved in seen or not os.access(resolved, os.X_OK):
            continue
        seen.add(resolved)
        try:
            probe = subprocess.run(
                [
                    resolved,
                    "-c",
                    "from importlib.metadata import version; "
                    "raise SystemExit(0 if version('PyYAML') == '6.0.3' else 1)",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return resolved
    return os.fspath(sys.executable)


def _scrub_memory_context_response(response: ModelResponse) -> ModelResponse:
    """Remove private recalled-memory fences before any worker/ledger exposure."""

    content = sanitize_context(response.content)
    completion = response.completion
    if completion is not None:
        completion = replace(completion, summary=sanitize_context(completion.summary))
    if content == response.content and completion is response.completion:
        return response
    return replace(response, content=content, completion=completion)


def _model_visible_tool_schemas(
    payload: Mapping[str, Any],
    granted_schemas: tuple[ToolSchema, ...],
) -> tuple[ToolSchema, ...]:
    """Validate the child-selected view of an already-approved tool surface."""

    granted = {schema.name: schema for schema in granted_schemas}
    raw_tools = payload.get("tools")
    if not isinstance(raw_tools, list):
        return granted_schemas
    visible: list[ToolSchema] = []
    seen: set[str] = set()
    for raw in raw_tools:
        if not isinstance(raw, dict):
            raise FoundationProtocolError("worker tool schema must be an object")
        function = raw.get("function")
        if not isinstance(function, dict):
            raise FoundationProtocolError("worker tool schema has no function object")
        name = function.get("name")
        if not isinstance(name, str) or not name or name in seen:
            raise FoundationProtocolError("worker tool schema has an invalid name")
        if name in granted:
            visible.append(granted[name])
            seen.add(name)
            continue
        if name not in _TOOL_DISCLOSURE_BRIDGE_NAMES | _WORKER_LOCAL_TOOL_NAMES:
            raise FoundationProtocolError("worker introduced an unapproved tool")
        description = function.get("description")
        parameters = function.get("parameters")
        if not isinstance(description, str) or not isinstance(parameters, dict):
            raise FoundationProtocolError("worker bridge schema is invalid")
        if len(description) > 4_000:
            raise FoundationProtocolError("worker bridge description exceeds the bound")
        visible.append(ToolSchema(name, description, parameters))
        seen.add(name)
    return tuple(visible)


def _package_root() -> Path:
    return Path(__file__).parents[1].resolve()


def _employee_runtime_core_root() -> Path:
    """Return the exact full baseline executed by the employee worker."""

    return Path(__file__).parents[1] / "_vendor" / "hermes_agent" / "upstream"


def _project_worker_code(home: Path) -> Path:
    """Expose only the Noruct package, never the parent's whole site-packages."""

    projection = home / "noruct-code"
    projection.mkdir(parents=True, exist_ok=True, mode=0o700)
    package_link = projection / "dynamic_firm"
    expected = _package_root()
    if package_link.is_symlink():
        if package_link.resolve() != expected:
            raise RuntimeError("Employee worker code projection changed")
    elif package_link.exists():
        raise RuntimeError("Employee worker code projection is not isolated")
    else:
        package_link.symlink_to(expected, target_is_directory=True)
    return projection


def _safe_namespace(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
