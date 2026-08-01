from __future__ import annotations

import asyncio
import fnmatch
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from dynamic_firm._vendor.runtime_safety.file_safety import get_read_block_error

from .models import ActionPolicy, IdempotencyMode, ToolEffect, ToolRisk
from .ports import CancellationToken
from .tool_contracts import ToolDefinition, ToolValidationError

def _require_string(arguments: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ToolValidationError(f"{key} must be a non-empty string")
    return value


class FixtureReader:
    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = dict(values)
        self.call_count = 0

    def definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            key = _require_string(arguments, "key")
            if set(arguments) != {"key"}:
                raise ToolValidationError("read_fixture accepts only the key field")
            return {"key": key}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            self.call_count += 1
            key = str(arguments["key"])
            if key not in self.values:
                raise KeyError(key)
            return self.values[key]

        return ToolDefinition(
            name="read_fixture",
            description="Read one named value from the deterministic test fixture.",
            input_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=validate,
            resource_key=lambda arguments: f"fixture:{arguments['key']}",
            handler=handle,
            parallel_safe=True,
        )


class WorkspaceReadTools:
    def __init__(
        self,
        workspaces: Mapping[str, Path],
        *,
        max_file_bytes: int = 64_000,
        max_entries: int = 500,
    ) -> None:
        self.workspaces = {key: value.resolve() for key, value in workspaces.items()}
        self.max_file_bytes = max_file_bytes
        self.max_entries = max_entries
        self.read_call_count = 0
        self.list_call_count = 0

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._read_definition(), self._list_definition(), self._search_definition()

    def _validate_path(self, arguments: Mapping[str, Any], *, allow_dot: bool) -> Mapping[str, Any]:
        workspace_id = _require_string(arguments, "workspace_id")
        raw_path = _require_string(arguments, "path", allow_empty=allow_dot)
        if workspace_id not in self.workspaces:
            raise ToolValidationError("Unknown workspace_id")
        path = PurePosixPath(raw_path or ".")
        if path.is_absolute() or ".." in path.parts:
            raise ToolValidationError("Path must stay inside the workspace")
        if any(part in {"", "."} for part in path.parts) and str(path) != ".":
            raise ToolValidationError("Path contains an invalid segment")
        return {"workspace_id": workspace_id, "path": str(path)}

    def _resolve(self, workspace_id: str, raw_path: str) -> Path:
        root = self.workspaces[workspace_id]
        candidate = (root / raw_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ToolValidationError("Resolved path escapes the workspace") from exc
        return candidate

    def _resource_key(self, arguments: Mapping[str, Any]) -> str:
        return f"workspace:{arguments['workspace_id']}:{arguments['path']}"

    def _read_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) != {"workspace_id", "path"}:
                raise ToolValidationError("read_workspace_file requires workspace_id and path")
            return self._validate_path(arguments, allow_dot=False)

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            from dynamic_firm.foundation.hermes_file_search import read_workspace

            cancellation.raise_if_cancelled()
            path = self._resolve(str(arguments["workspace_id"]), str(arguments["path"]))
            # This exact-pinned source-derived guard is defense in depth.  The
            # first-party workspace root, grant, symlink and size checks remain
            # the authority boundary; a worker never receives the upstream
            # file tool or a raw path outside that parent-owned scope.
            if get_read_block_error(str(path)):
                raise ToolValidationError(
                    "Sensitive or secret-bearing workspace files cannot be read"
                )
            if path.is_symlink() or not path.is_file():
                raise ToolValidationError("Path is not a regular file")
            if path.stat().st_size > self.max_file_bytes:
                raise ToolValidationError("File exceeds the read limit")
            data = path.read_bytes()
            if b"\x00" in data:
                raise ToolValidationError("Binary files are not readable")
            result = await asyncio.to_thread(
                read_workspace,
                root=self.workspaces[str(arguments["workspace_id"])],
                path=str(arguments["path"]),
            )
            self.read_call_count += 1
            if result.get("error"):
                raise ToolValidationError("Vendored workspace reader rejected the approved file")
            content = result.get("content")
            if not isinstance(content, str):
                raise ToolValidationError("Vendored workspace reader returned no text content")
            return content

        return ToolDefinition(
            name="read_workspace_file",
            description="Read one UTF-8 file inside an approved workspace root.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["workspace_id", "path"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=validate,
            resource_key=self._resource_key,
            handler=handle,
            parallel_safe=True,
        )

    def _list_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) != {"workspace_id", "path"}:
                raise ToolValidationError("list_workspace_files requires workspace_id and path")
            return self._validate_path(arguments, allow_dot=True)

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            base = self._resolve(str(arguments["workspace_id"]), str(arguments["path"]))
            if base.is_symlink() or not base.is_dir():
                raise ToolValidationError("Path is not a directory")
            root = self.workspaces[str(arguments["workspace_id"])]
            pending = [base]
            found: list[str] = []
            while pending:
                cancellation.raise_if_cancelled()
                current = pending.pop()
                for child in sorted(current.iterdir(), key=lambda item: item.name):
                    if child.is_symlink():
                        continue
                    if child.is_dir():
                        pending.append(child)
                    elif child.is_file():
                        found.append(child.relative_to(root).as_posix())
                        if len(found) > self.max_entries:
                            raise ToolValidationError("Workspace listing exceeds the entry limit")
            self.list_call_count += 1
            return json.dumps(sorted(found), ensure_ascii=False)

        return ToolDefinition(
            name="list_workspace_files",
            description="List regular files below an approved workspace directory.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["workspace_id", "path"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=validate,
            resource_key=self._resource_key,
            handler=handle,
            parallel_safe=True,
        )

    def _search_definition(self) -> ToolDefinition:
        """Expose the exact vendored file-operation search behind this root."""

        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            permitted = {
                "workspace_id",
                "path",
                "pattern",
                "target",
                "file_glob",
                "limit",
                "offset",
                "output_mode",
                "context",
            }
            if not set(arguments).issubset(permitted):
                raise ToolValidationError("search_workspace_files received unknown fields")
            if not {"workspace_id", "path", "pattern"}.issubset(arguments):
                raise ToolValidationError(
                    "search_workspace_files requires workspace_id, path, and pattern"
                )
            validated = dict(self._validate_path(arguments, allow_dot=True))
            pattern = _require_string(arguments, "pattern")
            target = str(arguments.get("target") or "content")
            if target not in {"content", "files"}:
                raise ToolValidationError("target must be content or files")
            file_glob = arguments.get("file_glob")
            if file_glob is not None and (not isinstance(file_glob, str) or len(file_glob) > 256):
                raise ToolValidationError("file_glob must be a bounded string")
            output_mode = str(arguments.get("output_mode") or "content")
            if output_mode not in {"content", "files_only", "count"}:
                raise ToolValidationError("output_mode is invalid")

            def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
                value = arguments.get(name, default)
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ToolValidationError(f"{name} must be an integer")
                if not minimum <= value <= maximum:
                    raise ToolValidationError(f"{name} is outside the allowed range")
                return value

            return {
                **validated,
                "pattern": pattern,
                "target": target,
                "file_glob": file_glob,
                "limit": bounded_int("limit", 50, 1, min(500, self.max_entries)),
                "offset": bounded_int("offset", 0, 0, 10_000),
                "output_mode": output_mode,
                "context": bounded_int("context", 0, 0, 20),
            }

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            from dynamic_firm.foundation.hermes_file_search import search_workspace

            cancellation.raise_if_cancelled()
            workspace_id = str(arguments["workspace_id"])
            relative_path = str(arguments["path"])
            base = self._resolve(workspace_id, relative_path)
            if base.is_symlink() or not base.exists():
                raise ToolValidationError("Search path is not an approved workspace entry")
            result = await asyncio.to_thread(
                search_workspace,
                root=self.workspaces[workspace_id],
                path=relative_path,
                pattern=str(arguments["pattern"]),
                target=str(arguments["target"]),
                file_glob=(str(arguments["file_glob"]) if arguments["file_glob"] is not None else None),
                limit=int(arguments["limit"]),
                offset=int(arguments["offset"]),
                output_mode=str(arguments["output_mode"]),
                context=int(arguments["context"]),
            )
            cancellation.raise_if_cancelled()
            return json.dumps(result, ensure_ascii=False, sort_keys=True)

        return ToolDefinition(
            name="search_workspace_files",
            description=(
                "Search file contents or filenames below an approved workspace path. "
                "Uses the vendored workspace search implementation with bounded results."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "pattern": {"type": "string"},
                    "target": {"type": "string", "enum": ["content", "files"], "default": "content"},
                    "file_glob": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": min(500, self.max_entries)},
                    "offset": {"type": "integer", "minimum": 0},
                    "output_mode": {"type": "string", "enum": ["content", "files_only", "count"]},
                    "context": {"type": "integer", "minimum": 0, "maximum": 20},
                },
                "required": ["workspace_id", "path", "pattern"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=validate,
            resource_key=self._resource_key,
            handler=handle,
            timeout_ms=20_000,
            output_limit_bytes=128_000,
            parallel_safe=True,
        )


_PROTECTED_SEGMENTS = {
    ".codex",
    ".git",
    ".hg",
    ".noruct",
    ".ssh",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
}
_PROTECTED_FILE_NAMES = {
    ".env",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}

