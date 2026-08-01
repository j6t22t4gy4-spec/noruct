from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import signal
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .background_process import BackgroundProcessRegistry, DEFAULT_BACKGROUND_PROCESS_REGISTRY
from .models import ActionPolicy, IdempotencyMode, ToolEffect, ToolRisk
from .ports import CancellationToken
from .tool_contracts import (
    ToolDefinition,
    ToolEffectNotStarted,
    ToolValidationError,
)
from .workspace_background_tools import WorkspaceBackgroundToolMixin
from .workspace_read_tools import WorkspaceReadTools, _require_string


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

def validate_workspace_mutation_path(raw_path: str) -> PurePosixPath:
    """Validate a first-party workspace mutation path without resolving it."""

    path = PurePosixPath(raw_path)
    if not raw_path or path.is_absolute() or ".." in path.parts:
        raise ToolValidationError("Path must stay inside the workspace")
    if any(part in {"", "."} for part in path.parts):
        raise ToolValidationError("Path contains an invalid segment")
    lowered = [part.lower() for part in path.parts]
    if any(part in _PROTECTED_SEGMENTS for part in lowered):
        raise ToolValidationError("Protected workspace paths cannot be modified")
    name = path.name.lower()
    if name in _PROTECTED_FILE_NAMES or name.startswith(".env."):
        raise ToolValidationError("Secret-bearing workspace files cannot be modified")
    return path


def checked_workspace_mutation_target(root: Path, raw_path: str) -> Path:
    """Resolve a mutation target while rejecting symlink traversal and escapes."""

    relative = validate_workspace_mutation_path(raw_path)
    resolved_root = root.resolve()
    current = resolved_root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ToolValidationError("Mutation paths cannot traverse symbolic links")
    target = (resolved_root / relative).resolve()
    try:
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise ToolValidationError("Resolved path escapes the workspace") from exc
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise ToolValidationError("Mutation target must be a regular file")
    return target


def atomic_write_text(path: Path, content: str) -> None:
    """Replace one UTF-8 file without exposing a partially written target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    descriptor, temporary = tempfile.mkstemp(prefix=".noruct-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
_BLOCKED_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|[;&|]\s*)\s*(sudo|doas)\b", re.I), "privilege escalation is blocked"),
    (re.compile(r"(^|[;&|]\s*)\s*(ssh|scp|sftp|telnet|nc|netcat)\b", re.I), "remote communication is blocked"),
    (re.compile(r"\b(curl|wget)\b", re.I), "network download commands are blocked"),
    (re.compile(r"\bgit\s+push\b", re.I), "remote git mutation is blocked"),
    (re.compile(r"\bgh\s+(pr\s+merge|release|api)\b", re.I), "external GitHub mutation is blocked"),
    (re.compile(r"\b(npm|pnpm|yarn)\s+(publish|install|add|remove|update|upgrade)\b", re.I), "package network or install mutation is blocked"),
    (re.compile(r"\b(pip|pip3|uv)\s+(install|uninstall|add|remove|sync)\b", re.I), "package environment mutation is blocked"),
    (re.compile(r"\b(brew|apt|apt-get|dnf|yum|pacman)\b", re.I), "system package mutation is blocked"),
    (re.compile(r"(^|[;&|]\s*)\s*(rm|rmdir|unlink|shred)\b", re.I), "destructive file deletion is blocked"),
    (re.compile(r"\bfind\b[^;&|]*\s-delete\b", re.I), "destructive file deletion is blocked"),
    (re.compile(r"\bgit\s+(reset\s+--hard|clean\b|checkout\s+--|restore\b)", re.I), "destructive git recovery is blocked"),
    (re.compile(r"(^|[;&|]\s*)\s*(chmod|chown|kill|pkill|killall)\b", re.I), "host permission or process mutation is blocked"),
    (re.compile(r"\b(shutdown|reboot|launchctl|systemctl)\b", re.I), "host service mutation is blocked"),
    (re.compile(r"\b(docker|podman)\b", re.I), "container host control is blocked"),
)


class WorkspaceTools(WorkspaceReadTools, WorkspaceBackgroundToolMixin):
    """Bounded workspace coding tools for an explicitly approved interactive run.

    Command execution is host-backed and intentionally not described as a sandbox.
    Every mutation definition requires a separate approval at the ToolExecutor boundary.
    """

    def __init__(
        self,
        workspaces: Mapping[str, Path],
        *,
        max_file_bytes: int = 64_000,
        max_entries: int = 500,
        max_write_bytes: int = 256_000,
        max_command_bytes: int = 4_096,
        max_command_output_bytes: int = 64_000,
        max_command_timeout_seconds: float = 120.0,
        environ: Mapping[str, str] | None = None,
        shell: str = "/bin/sh",
        background_registry: BackgroundProcessRegistry | None = None,
    ) -> None:
        super().__init__(
            workspaces,
            max_file_bytes=max_file_bytes,
            max_entries=max_entries,
        )
        self.max_write_bytes = max_write_bytes
        self.max_command_bytes = max_command_bytes
        self.max_command_output_bytes = max_command_output_bytes
        self.max_command_timeout_seconds = max_command_timeout_seconds
        self.environ = dict(os.environ if environ is None else environ)
        self.shell = shell
        self.background_registry = background_registry or DEFAULT_BACKGROUND_PROCESS_REGISTRY

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (
            *super().definitions(),
            self._write_definition(),
            self._edit_definition(),
            self._patch_definition(),
            self._multi_patch_definition(),
            self._move_definition(),
            self._delete_definition(),
            self._command_definition(),
            self._background_start_definition(),
            self._background_list_definition(),
            self._background_inspect_definition(),
            self._background_wait_definition(),
            self._background_stdin_definition(),
            self._background_stop_definition(),
        )

    def _validate_mutation_path(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        validated = self._validate_path(arguments, allow_dot=False)
        validate_workspace_mutation_path(str(validated["path"]))
        return validated

    def _checked_mutation_target(self, workspace_id: str, raw_path: str) -> Path:
        return checked_workspace_mutation_target(self.workspaces[workspace_id], raw_path)

    def _background_workspace_key(self, workspace_id: str) -> str:
        return str(self.workspaces[workspace_id].resolve())

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        atomic_write_text(path, content)

    def _write_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) != {"workspace_id", "path", "content"}:
                raise ToolValidationError(
                    "write_workspace_file requires workspace_id, path, and content"
                )
            validated = self._validate_mutation_path(arguments)
            content = _require_string(arguments, "content", allow_empty=True)
            if len(content.encode("utf-8")) > self.max_write_bytes:
                raise ToolValidationError("File content exceeds the write byte limit")
            return {**validated, "content": content}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            # The parent validates the target and owns the write approval. The
            # exact source backend then supplies its atomic write, verification
            # and best-effort diagnostic behavior.
            from dynamic_firm.foundation.hermes_file_search import write_workspace

            cancellation.raise_if_cancelled()
            workspace_id = str(arguments["workspace_id"])
            self._checked_mutation_target(workspace_id, str(arguments["path"]))
            content = str(arguments["content"])
            result = await asyncio.to_thread(
                write_workspace,
                root=self.workspaces[workspace_id],
                path=str(arguments["path"]),
                content=content,
            )
            cancellation.raise_if_cancelled()
            if result.get("error"):
                raise ToolValidationError(str(result["error"]))
            return json.dumps({"path": str(arguments["path"]), **result}, ensure_ascii=False, sort_keys=True)

        return ToolDefinition(
            name="write_workspace_file",
            description=(
                "Create or completely replace one UTF-8 file inside the approved workspace. "
                "Use edit_workspace_file for targeted changes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["workspace_id", "path", "content"],
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            risk=ToolRisk.MEDIUM,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=self._resource_key,
            handler=handle,
            timeout_ms=10_000,
            requires_approval=True,
            allow_session_approval=True,
            approval_preview=lambda arguments: (
                f"Write {arguments['path']} "
                f"({len(str(arguments['content']).encode('utf-8'))} bytes)"
            ),
        )

    def _edit_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            required = {"workspace_id", "path", "old_text", "new_text"}
            if set(arguments) != required:
                raise ToolValidationError(
                    "edit_workspace_file requires workspace_id, path, old_text, and new_text"
                )
            validated = self._validate_mutation_path(arguments)
            old_text = _require_string(arguments, "old_text")
            new_text = _require_string(arguments, "new_text", allow_empty=True)
            if len(new_text.encode("utf-8")) > self.max_write_bytes:
                raise ToolValidationError("Replacement text exceeds the write byte limit")
            return {**validated, "old_text": old_text, "new_text": new_text}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            from dynamic_firm.foundation.hermes_file_search import patch_workspace

            cancellation.raise_if_cancelled()
            workspace_id = str(arguments["workspace_id"])
            try:
                target = self._checked_mutation_target(
                    workspace_id,
                    str(arguments["path"]),
                )
                if not target.is_file():
                    raise ToolValidationError("Edit target does not exist")
                if target.stat().st_size > self.max_write_bytes:
                    raise ToolValidationError("Edit target exceeds the write byte limit")
                data = await asyncio.to_thread(target.read_bytes)
                if b"\x00" in data:
                    raise ToolValidationError("Binary files cannot be edited")
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ToolValidationError("Edit target is not UTF-8") from exc
                old_text = str(arguments["old_text"])
                matches = content.count(old_text)
                if matches != 1:
                    raise ToolValidationError(
                        "old_text must match exactly once in the original file; "
                        f"found {matches}"
                    )
                updated = content.replace(
                    old_text,
                    str(arguments["new_text"]),
                    1,
                )
                if len(updated.encode("utf-8")) > self.max_write_bytes:
                    raise ToolValidationError(
                        "Edited file exceeds the write byte limit"
                    )
            except ToolValidationError as exc:
                raise ToolEffectNotStarted(str(exc)) from exc
            result = await asyncio.to_thread(
                patch_workspace,
                root=self.workspaces[workspace_id],
                path=str(arguments["path"]),
                old_string=old_text,
                new_string=str(arguments["new_text"]),
            )
            cancellation.raise_if_cancelled()
            if not result.get("success"):
                raise ToolValidationError(str(result.get("error") or "Source edit was not applied"))
            return json.dumps({"path": str(arguments["path"]), "replacements": 1, **result}, ensure_ascii=False, sort_keys=True)

        return ToolDefinition(
            name="edit_workspace_file",
            description=(
                "Replace one exact, unique text block in an existing UTF-8 workspace file. "
                "The old_text must occur exactly once in the original file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["workspace_id", "path", "old_text", "new_text"],
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            risk=ToolRisk.MEDIUM,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=self._resource_key,
            handler=handle,
            timeout_ms=10_000,
            requires_approval=True,
            allow_session_approval=True,
            approval_preview=lambda arguments: (
                f"Edit {arguments['path']} "
                f"({len(str(arguments['old_text']))} → {len(str(arguments['new_text']))} characters)"
            ),
        )

    def _patch_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            allowed = {"workspace_id", "path", "old_text", "new_text", "replace_all"}
            required = {"workspace_id", "path", "old_text", "new_text"}
            if not required.issubset(arguments) or not set(arguments).issubset(allowed):
                raise ToolValidationError(
                    "patch_workspace_file requires workspace_id, path, old_text, and new_text"
                )
            validated = self._validate_mutation_path(arguments)
            old_text = _require_string(arguments, "old_text")
            new_text = _require_string(arguments, "new_text", allow_empty=True)
            replace_all = arguments.get("replace_all", False)
            if not isinstance(replace_all, bool):
                raise ToolValidationError("replace_all must be a boolean")
            if len(old_text.encode("utf-8")) > self.max_write_bytes:
                raise ToolValidationError("Original text exceeds the patch byte limit")
            if len(new_text.encode("utf-8")) > self.max_write_bytes:
                raise ToolValidationError("Replacement text exceeds the patch byte limit")
            return {
                **validated,
                "old_text": old_text,
                "new_text": new_text,
                "replace_all": replace_all,
            }

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            # Import lazily: the private bridge lives in the foundation package,
            # whose public initializer also exposes the runtime that imports this
            # module. The source engine remains unavailable until a parent-owned
            # action has passed validation and approval.
            from dynamic_firm.foundation.hermes_file_search import patch_workspace

            cancellation.raise_if_cancelled()
            workspace_id = str(arguments["workspace_id"])
            target = self._checked_mutation_target(workspace_id, str(arguments["path"]))
            if not target.is_file():
                raise ToolValidationError("Patch target does not exist")
            if target.stat().st_size > self.max_write_bytes:
                raise ToolValidationError("Patch target exceeds the write byte limit")
            data = await asyncio.to_thread(target.read_bytes)
            if b"\x00" in data:
                raise ToolValidationError("Binary files cannot be patched")
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolValidationError("Patch target is not UTF-8") from exc
            result = await asyncio.to_thread(
                patch_workspace,
                root=self.workspaces[workspace_id],
                path=str(arguments["path"]),
                old_string=str(arguments["old_text"]),
                new_string=str(arguments["new_text"]),
                replace_all=bool(arguments["replace_all"]),
            )
            cancellation.raise_if_cancelled()
            if not result.get("success"):
                raise ToolValidationError(str(result.get("error") or "Source patch was not applied"))
            return json.dumps(result, ensure_ascii=False, sort_keys=True)

        return ToolDefinition(
            name="patch_workspace_file",
            description=(
                "Apply a targeted fuzzy text replacement to one existing UTF-8 workspace file. "
                "Use this when a small formatting difference makes an exact edit impractical."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["workspace_id", "path", "old_text", "new_text"],
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            risk=ToolRisk.MEDIUM,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=self._resource_key,
            handler=handle,
            timeout_ms=20_000,
            requires_approval=True,
            allow_session_approval=True,
            approval_preview=lambda arguments: (
                f"Patch {arguments['path']} "
                f"({len(str(arguments['old_text']))} → {len(str(arguments['new_text']))} characters)"
            ),
        )

    def _multi_patch_definition(self) -> ToolDefinition:
        """Expose source V4A patches only after complete parent preflight.

        The source parser is deliberately used for the grammar, but it never
        decides the workspace boundary, allowed target class, approval or
        policy.  Parsing is read-only and happens before an approval request;
        applying happens only after the normal parent ToolExecutor checkpoint.
        """

        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) != {"workspace_id", "patch"}:
                raise ToolValidationError(
                    "apply_workspace_multi_patch requires workspace_id and patch"
                )
            workspace_id = _require_string(arguments, "workspace_id")
            if workspace_id not in self.workspaces:
                raise ToolValidationError(f"Unknown workspace: {workspace_id}")
            patch = _require_string(arguments, "patch")
            if len(patch.encode("utf-8")) > self.max_write_bytes:
                raise ToolValidationError("Patch exceeds the write byte limit")

            from dynamic_firm.foundation.hermes_file_search import parse_workspace_v4a_patch

            parsed = parse_workspace_v4a_patch(
                root=self.workspaces[workspace_id], patch=patch
            )
            if parsed.get("error"):
                raise ToolValidationError(str(parsed["error"]))
            operations = parsed.get("operations")
            if not isinstance(operations, list) or not operations:
                raise ToolValidationError("Patch contains no file operations")
            if len(operations) > 32:
                raise ToolValidationError("Patch exceeds the 32-file operation limit")

            root = self.workspaces[workspace_id]
            prepared: list[dict[str, object]] = []
            for item in operations:
                if not isinstance(item, Mapping):
                    raise ToolValidationError("Source patch parser returned an invalid operation")
                operation = str(item.get("operation") or "")
                raw_path = item.get("path")
                if operation not in {"add", "update", "delete", "move"} or not isinstance(raw_path, str):
                    raise ToolValidationError("Source patch parser returned an invalid operation")
                target = checked_workspace_mutation_target(root, raw_path)
                if operation in {"update", "delete", "move"}:
                    if not target.is_file():
                        raise ToolValidationError(f"{operation} target does not exist: {raw_path}")
                    if target.stat().st_size > self.max_write_bytes:
                        raise ToolValidationError(f"{operation} target exceeds the write byte limit: {raw_path}")
                    try:
                        data = target.read_bytes()
                    except OSError as exc:
                        raise ToolValidationError(f"Cannot inspect {raw_path}") from exc
                    if b"\x00" in data:
                        raise ToolValidationError(f"Binary files cannot be patched: {raw_path}")
                    try:
                        data.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ToolValidationError(f"Patch target is not UTF-8: {raw_path}") from exc
                if operation == "add" and target.exists():
                    raise ToolValidationError(f"Add target already exists: {raw_path}")

                record: dict[str, object] = {
                    "operation": operation,
                    "path": raw_path,
                    "hunk_count": int(item.get("hunk_count") or 0),
                }
                if operation == "move":
                    new_path = item.get("new_path")
                    if not isinstance(new_path, str):
                        raise ToolValidationError("Move operation has no destination path")
                    new_target = checked_workspace_mutation_target(root, new_path)
                    if new_target.exists():
                        raise ToolValidationError(f"Move destination already exists: {new_path}")
                    record["new_path"] = new_path
                prepared.append(record)
            return {
                "workspace_id": workspace_id,
                "patch": patch,
                "operations": tuple(prepared),
            }

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            from dynamic_firm.foundation.hermes_file_search import apply_workspace_v4a_patch

            cancellation.raise_if_cancelled()
            workspace_id = str(arguments["workspace_id"])
            result = await asyncio.to_thread(
                apply_workspace_v4a_patch,
                root=self.workspaces[workspace_id],
                patch=str(arguments["patch"]),
            )
            cancellation.raise_if_cancelled()
            if not result.get("success"):
                raise ToolValidationError(str(result.get("error") or "Source multi-file patch was not applied"))
            # The source result is bounded by ToolDefinition output handling;
            # return the audited parent operation list without echoing raw patch.
            return json.dumps(
                {"operations": arguments["operations"], **result},
                ensure_ascii=False,
                sort_keys=True,
            )

        def preview(arguments: Mapping[str, Any]) -> str:
            operations = arguments["operations"]
            labels = []
            for item in operations[:5]:
                path = str(item["path"])
                labels.append(
                    f"{item['operation']} {path}"
                    + (f" → {item['new_path']}" if item.get("new_path") else "")
                )
            suffix = "" if len(operations) <= 5 else f"; +{len(operations) - 5} more"
            return f"Apply multi-file patch ({len(operations)} operation(s)): " + "; ".join(labels) + suffix

        return ToolDefinition(
            name="apply_workspace_multi_patch",
            description=(
                "Apply an approved V4A multi-file patch inside the workspace. "
                "Supports add, update, delete and move after all targets pass parent validation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "patch": {"type": "string"},
                },
                "required": ["workspace_id", "patch"],
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            risk=ToolRisk.HIGH,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=lambda arguments: (
                f"workspace:{arguments['workspace_id']}:multi-patch:"
                f"{hashlib.sha256(str(arguments['patch']).encode('utf-8')).hexdigest()[:16]}"
            ),
            handler=handle,
            timeout_ms=30_000,
            output_limit_bytes=128_000,
            requires_approval=True,
            allow_session_approval=True,
            approval_preview=preview,
        )

    def _move_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) != {"workspace_id", "source_path", "destination_path"}:
                raise ToolValidationError(
                    "move_workspace_file requires workspace_id, source_path, and destination_path"
                )
            workspace_id = _require_string(arguments, "workspace_id")
            if workspace_id not in self.workspaces:
                raise ToolValidationError("Unknown workspace_id")
            source_path = _require_string(arguments, "source_path")
            destination_path = _require_string(arguments, "destination_path")
            root = self.workspaces[workspace_id]
            source = checked_workspace_mutation_target(root, source_path)
            destination = checked_workspace_mutation_target(root, destination_path)
            if not source.is_file():
                raise ToolValidationError("Move source does not exist")
            if source.stat().st_size > self.max_write_bytes:
                raise ToolValidationError("Move source exceeds the write byte limit")
            if destination.exists():
                raise ToolValidationError("Move destination already exists")
            return {
                "workspace_id": workspace_id,
                "source_path": source_path,
                "destination_path": destination_path,
            }

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            from dynamic_firm.foundation.hermes_file_search import move_workspace

            cancellation.raise_if_cancelled()
            workspace_id = str(arguments["workspace_id"])
            result = await asyncio.to_thread(
                move_workspace,
                root=self.workspaces[workspace_id],
                source=str(arguments["source_path"]),
                destination=str(arguments["destination_path"]),
            )
            cancellation.raise_if_cancelled()
            if result.get("error"):
                raise ToolValidationError(str(result["error"]))
            return json.dumps(
                {
                    "source_path": arguments["source_path"],
                    "destination_path": arguments["destination_path"],
                    **result,
                },
                ensure_ascii=False,
                sort_keys=True,
            )

        return ToolDefinition(
            name="move_workspace_file",
            description="Move or rename one approved regular file inside the workspace without overwriting a destination.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "source_path": {"type": "string"},
                    "destination_path": {"type": "string"},
                },
                "required": ["workspace_id", "source_path", "destination_path"],
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            risk=ToolRisk.HIGH,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=lambda arguments: (
                f"workspace:{arguments['workspace_id']}:move:{arguments['source_path']}"
            ),
            handler=handle,
            timeout_ms=15_000,
            requires_approval=True,
            allow_session_approval=True,
            approval_preview=lambda arguments: (
                f"Move {arguments['source_path']} → {arguments['destination_path']}"
            ),
        )

    def _delete_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) != {"workspace_id", "path"}:
                raise ToolValidationError("delete_workspace_file requires workspace_id and path")
            validated = self._validate_mutation_path(arguments)
            target = self._checked_mutation_target(
                str(validated["workspace_id"]), str(validated["path"])
            )
            if not target.is_file():
                raise ToolValidationError("Delete target does not exist")
            if target.stat().st_size > self.max_write_bytes:
                raise ToolValidationError("Delete target exceeds the write byte limit")
            return validated

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            from dynamic_firm.foundation.hermes_file_search import delete_workspace

            cancellation.raise_if_cancelled()
            workspace_id = str(arguments["workspace_id"])
            result = await asyncio.to_thread(
                delete_workspace,
                root=self.workspaces[workspace_id],
                path=str(arguments["path"]),
            )
            cancellation.raise_if_cancelled()
            if result.get("error"):
                raise ToolValidationError(str(result["error"]))
            return json.dumps({"path": arguments["path"], **result}, ensure_ascii=False, sort_keys=True)

        return ToolDefinition(
            name="delete_workspace_file",
            description="Delete one approved regular file inside the workspace. Directories and protected paths are not supported.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["workspace_id", "path"],
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            risk=ToolRisk.HIGH,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=lambda arguments: (
                f"workspace:{arguments['workspace_id']}:delete:{arguments['path']}"
            ),
            handler=handle,
            timeout_ms=15_000,
            requires_approval=True,
            allow_session_approval=True,
            approval_preview=lambda arguments: f"Delete {arguments['path']}",
        )

    def _validate_command(self, command: str) -> None:
        if "\n" in command or "\r" in command or "\x00" in command:
            raise ToolValidationError("Command must be one bounded line")
        if len(command.encode("utf-8")) > self.max_command_bytes:
            raise ToolValidationError("Command exceeds the byte limit")
        if "$(" in command or "`" in command:
            raise ToolValidationError("Command substitution is blocked")
        if "$HOME" in command or "${HOME}" in command or "~/" in command or "../" in command:
            raise ToolValidationError("Command path escapes are blocked")
        for pattern, message in _BLOCKED_COMMAND_PATTERNS:
            if pattern.search(command):
                raise ToolValidationError(message)
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError as exc:
            raise ToolValidationError("Command quoting is invalid") from exc
        if not tokens:
            raise ToolValidationError("Command must be non-empty")
        for token in tokens:
            if token.startswith("/"):
                raise ToolValidationError("Absolute command paths are blocked")

    def _command_environment(self, root: Path) -> dict[str, str]:
        exact = {
            "PATH",
            "USER",
            "LOGNAME",
            "SHELL",
            "LANG",
            "TERM",
            "TMPDIR",
            "VIRTUAL_ENV",
            "PYTHONPATH",
            "PYTHONHOME",
            "GIT_CONFIG_NOSYSTEM",
        }
        prefixes = ("LC_", "XDG_", "PYENV_", "NVM_")
        environment = {
            key: value
            for key, value in self.environ.items()
            if key in exact or key.startswith(prefixes)
        }
        # Do not expose the user's real home directory to a host-backed command.
        # This is defense in depth, not an OS sandbox: the concrete command is
        # still shown to the user and requires approval every time.
        environment["HOME"] = str(root)
        return environment

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "posix" and process.pid:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
            return
        except TimeoutError:
            pass
        try:
            if os.name == "posix" and process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        await process.wait()

    async def _run_command(
        self,
        *,
        root: Path,
        command: str,
        timeout_seconds: float,
        cancellation: CancellationToken,
    ) -> str:
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            self.shell,
            "-c",
            command,
            cwd=root,
            env=self._command_environment(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=(os.name == "posix"),
        )
        collected = bytearray()
        truncated = False

        async def read_output() -> None:
            nonlocal truncated
            assert process.stdout is not None
            while True:
                chunk = await process.stdout.read(8_192)
                if not chunk:
                    return
                remaining = self.max_command_output_bytes - len(collected)
                if remaining > 0:
                    collected.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True

        reader = asyncio.create_task(read_output(), name="workspace-command-output")
        waiter = asyncio.create_task(process.wait(), name="workspace-command-wait")
        cancelled = asyncio.create_task(cancellation.wait(), name="workspace-command-cancel")
        try:
            done, _ = await asyncio.wait(
                {waiter, cancelled},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done:
                await self._terminate_process(process)
                raise OperationCancelled(cancellation.reason or "Command cancelled")
            if waiter not in done:
                await self._terminate_process(process)
                raise TimeoutError
            await reader
        finally:
            cancelled.cancel()
            if process.returncode is None:
                await self._terminate_process(process)
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, waiter, cancelled, return_exceptions=True)

        output = bytes(collected).decode("utf-8", errors="replace")
        return json.dumps(
            {
                "command": command,
                "exit_code": process.returncode,
                "output": output,
                "output_truncated": truncated,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _command_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if not set(arguments).issubset({"workspace_id", "command", "timeout_seconds"}):
                raise ToolValidationError("run_workspace_command received unknown fields")
            if "workspace_id" not in arguments or "command" not in arguments:
                raise ToolValidationError("run_workspace_command requires workspace_id and command")
            workspace_id = _require_string(arguments, "workspace_id")
            if workspace_id not in self.workspaces:
                raise ToolValidationError("Unknown workspace_id")
            command = _require_string(arguments, "command")
            self._validate_command(command)
            timeout = arguments.get("timeout_seconds", 30.0)
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
                raise ToolValidationError("timeout_seconds must be a number")
            timeout_seconds = float(timeout)
            if timeout_seconds <= 0 or timeout_seconds > self.max_command_timeout_seconds:
                raise ToolValidationError("timeout_seconds is outside the allowed range")
            return {
                "workspace_id": workspace_id,
                "command": command,
                "timeout_seconds": timeout_seconds,
            }

        def resource_key(arguments: Mapping[str, Any]) -> str:
            digest = hashlib.sha256(str(arguments["command"]).encode("utf-8")).hexdigest()[:16]
            return f"workspace:{arguments['workspace_id']}:command:{digest}"

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            return await self._run_command(
                root=self.workspaces[str(arguments["workspace_id"])],
                command=str(arguments["command"]),
                timeout_seconds=float(arguments["timeout_seconds"]),
                cancellation=cancellation,
            )

        return ToolDefinition(
            name="run_workspace_command",
            description=(
                "Run one bounded local command in the workspace to inspect, build, lint, or test it. "
                "This is host-backed, always requires user approval, and rejects common external "
                "or destructive command patterns without claiming sandbox isolation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": self.max_command_timeout_seconds},
                },
                "required": ["workspace_id", "command"],
                "additionalProperties": False,
            },
            effect=ToolEffect.EXECUTE,
            risk=ToolRisk.HIGH,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=resource_key,
            handler=handle,
            timeout_ms=int(self.max_command_timeout_seconds * 1000) + 1_000,
            output_limit_bytes=self.max_command_output_bytes + 2_048,
            requires_approval=True,
            approval_preview=lambda arguments: f"Run in workspace: {arguments['command']}",
        )
