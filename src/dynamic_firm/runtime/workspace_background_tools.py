from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any, Mapping

from .background_process import BackgroundProcessRegistry
from .models import ActionPolicy, IdempotencyMode, ToolEffect, ToolRisk
from .ports import CancellationToken
from .tool_contracts import ToolDefinition, ToolValidationError
from .workspace_read_tools import _require_string


class WorkspaceBackgroundToolMixin:
    """Expose bounded lifecycle tools for already-isolated workspace processes."""
    def _validate_background_process(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if set(arguments) != {"workspace_id", "process_id"}:
            raise ToolValidationError("Background process action requires workspace_id and process_id")
        workspace_id = _require_string(arguments, "workspace_id")
        if workspace_id not in self.workspaces:
            raise ToolValidationError("Unknown workspace_id")
        process_id = _require_string(arguments, "process_id")
        if not re.fullmatch(r"process-[0-9a-f]{16}", process_id):
            raise ToolValidationError("process_id is invalid")
        try:
            self.background_registry.inspect(
                workspace_key=self._background_workspace_key(workspace_id), process_id=process_id
            )
        except ValueError as exc:
            raise ToolValidationError(str(exc)) from exc
        return {"workspace_id": workspace_id, "process_id": process_id}

    @staticmethod
    def _background_resource_key(arguments: Mapping[str, Any]) -> str:
        return f"workspace:{arguments['workspace_id']}:process:{arguments['process_id']}"

    def _background_start_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if not set(arguments).issubset({"workspace_id", "command", "interactive"}):
                raise ToolValidationError(
                    "run_workspace_background_command received unknown fields"
                )
            workspace_id = _require_string(arguments, "workspace_id")
            if workspace_id not in self.workspaces:
                raise ToolValidationError("Unknown workspace_id")
            command = _require_string(arguments, "command")
            self._validate_command(command)
            interactive = arguments.get("interactive", False)
            if not isinstance(interactive, bool):
                raise ToolValidationError("interactive must be a boolean")
            return {"workspace_id": workspace_id, "command": command, "interactive": interactive}

        def resource_key(arguments: Mapping[str, Any]) -> str:
            digest = hashlib.sha256(str(arguments["command"]).encode("utf-8")).hexdigest()[:16]
            return f"workspace:{arguments['workspace_id']}:process-start:{digest}"

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            workspace_id = str(arguments["workspace_id"])
            item = await asyncio.to_thread(
                self.background_registry.start,
                workspace_key=self._background_workspace_key(workspace_id),
                command=str(arguments["command"]),
                cwd=str(self.workspaces[workspace_id]),
                environment=self._command_environment(self.workspaces[workspace_id]),
                shell=self.shell,
                interactive=bool(arguments["interactive"]),
            )
            cancellation.raise_if_cancelled()
            return json.dumps(
                self.background_registry.inspect(
                    workspace_key=self._background_workspace_key(workspace_id),
                    process_id=item.process_id,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )

        return ToolDefinition(
            name="run_workspace_background_command",
            description=(
                "Start one approved bounded command in the workspace and return a process_id. "
                "Set interactive=true only for a CLI or REPL that needs separately approved stdin; "
                "use process inspection, waiting, stdin, or stop tools to manage it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "command": {"type": "string"},
                    "interactive": {"type": "boolean", "default": False},
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
            timeout_ms=10_000,
            requires_approval=True,
            approval_preview=lambda arguments: f"Start background command: {arguments['command']}",
        )

    def _background_list_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) != {"workspace_id"}:
                raise ToolValidationError("list_workspace_processes requires workspace_id")
            workspace_id = _require_string(arguments, "workspace_id")
            if workspace_id not in self.workspaces:
                raise ToolValidationError("Unknown workspace_id")
            return {"workspace_id": workspace_id}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            return json.dumps(
                {
                    "processes": self.background_registry.list(
                        workspace_key=self._background_workspace_key(str(arguments["workspace_id"]))
                    )
                },
                ensure_ascii=False,
                sort_keys=True,
            )

        return ToolDefinition(
            name="list_workspace_processes",
            description="List background commands started in the approved workspace.",
            input_schema={
                "type": "object",
                "properties": {"workspace_id": {"type": "string"}},
                "required": ["workspace_id"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=validate,
            resource_key=lambda arguments: f"workspace:{arguments['workspace_id']}:processes",
            handler=handle,
            parallel_safe=True,
        )

    def _background_inspect_definition(self) -> ToolDefinition:
        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            return json.dumps(
                self.background_registry.inspect(
                    workspace_key=self._background_workspace_key(str(arguments["workspace_id"])),
                    process_id=str(arguments["process_id"]),
                    include_output=True,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )

        return ToolDefinition(
            name="inspect_workspace_process",
            description="Read status and bounded output from one background workspace command.",
            input_schema={
                "type": "object",
                "properties": {"workspace_id": {"type": "string"}, "process_id": {"type": "string"}},
                "required": ["workspace_id", "process_id"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=self._validate_background_process,
            resource_key=self._background_resource_key,
            handler=handle,
            output_limit_bytes=self.max_command_output_bytes + 2_048,
            parallel_safe=True,
        )

    def _background_wait_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if not set(arguments).issubset({"workspace_id", "process_id", "timeout_seconds"}):
                raise ToolValidationError("wait_workspace_process received unknown fields")
            base = self._validate_background_process(
                {key: value for key, value in arguments.items() if key != "timeout_seconds"}
            )
            timeout = arguments.get("timeout_seconds", 10.0)
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
                raise ToolValidationError("timeout_seconds must be a number")
            if not 0.1 <= float(timeout) <= 30.0:
                raise ToolValidationError("timeout_seconds is outside the allowed range")
            return {**base, "timeout_seconds": float(timeout)}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            result = await self.background_registry.wait(
                workspace_key=self._background_workspace_key(str(arguments["workspace_id"])),
                process_id=str(arguments["process_id"]),
                timeout_seconds=float(arguments["timeout_seconds"]),
                cancellation=cancellation,
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)

        return ToolDefinition(
            name="wait_workspace_process",
            description="Wait for one background workspace command without exceeding a short bound.",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "process_id": {"type": "string"},
                    "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 30},
                },
                "required": ["workspace_id", "process_id"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=validate,
            resource_key=self._background_resource_key,
            handler=handle,
            timeout_ms=31_000,
            output_limit_bytes=self.max_command_output_bytes + 2_048,
        )

    def _background_stdin_definition(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) != {"workspace_id", "process_id", "data"}:
                raise ToolValidationError(
                    "write_workspace_process_stdin requires workspace_id, process_id, and data"
                )
            base = self._validate_background_process(
                {key: value for key, value in arguments.items() if key != "data"}
            )
            data = _require_string(arguments, "data", allow_empty=False)
            if len(data.encode("utf-8")) > 32_768:
                raise ToolValidationError("Process stdin data exceeds the 32 KiB limit")
            try:
                snapshot = self.background_registry.inspect(
                    workspace_key=self._background_workspace_key(str(base["workspace_id"])),
                    process_id=str(base["process_id"]),
                )
            except ValueError as exc:  # Defensive: the registry is shared across runs.
                raise ToolValidationError(str(exc)) from exc
            if not bool(snapshot.get("interactive")):
                raise ToolValidationError("Process was not started as interactive")
            return {**base, "data": data}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            result = await asyncio.to_thread(
                self.background_registry.write_stdin,
                workspace_key=self._background_workspace_key(str(arguments["workspace_id"])),
                process_id=str(arguments["process_id"]),
                data=str(arguments["data"]),
            )
            cancellation.raise_if_cancelled()
            return json.dumps(result, ensure_ascii=False, sort_keys=True)

        return ToolDefinition(
            name="write_workspace_process_stdin",
            description=(
                "Send explicitly approved text to an interactive workspace process without appending "
                "a newline. Use only for a process started with interactive=true."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "process_id": {"type": "string"},
                    "data": {"type": "string", "maxLength": 32768},
                },
                "required": ["workspace_id", "process_id", "data"],
                "additionalProperties": False,
            },
            effect=ToolEffect.EXECUTE,
            risk=ToolRisk.HIGH,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=self._background_resource_key,
            handler=handle,
            timeout_ms=10_000,
            requires_approval=True,
            approval_preview=lambda arguments: (
                f"Send {len(str(arguments['data']).encode('utf-8'))} bytes to "
                f"interactive process {arguments['process_id']}"
            ),
        )

    def _background_stop_definition(self) -> ToolDefinition:
        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            result = await asyncio.to_thread(
                self.background_registry.stop,
                workspace_key=self._background_workspace_key(str(arguments["workspace_id"])),
                process_id=str(arguments["process_id"]),
            )
            cancellation.raise_if_cancelled()
            return json.dumps(result, ensure_ascii=False, sort_keys=True)

        return ToolDefinition(
            name="stop_workspace_process",
            description="Terminate one approved background workspace command and its process group.",
            input_schema={
                "type": "object",
                "properties": {"workspace_id": {"type": "string"}, "process_id": {"type": "string"}},
                "required": ["workspace_id", "process_id"],
                "additionalProperties": False,
            },
            effect=ToolEffect.EXECUTE,
            risk=ToolRisk.HIGH,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=self._validate_background_process,
            resource_key=self._background_resource_key,
            handler=handle,
            timeout_ms=10_000,
            requires_approval=True,
            approval_preview=lambda arguments: f"Stop background process {arguments['process_id']}",
        )
