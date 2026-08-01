"""Bounded user-managed external model-process transport.

This transport is for subscription/OAuth-capable CLIs that have no stable
public HTTP completion contract.  Noruct sends a versioned JSON request to
stdin and accepts one JSON response on stdout; credentials, browser login and
the external product's configuration remain entirely outside Noruct.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.providers.openai_compat import _parse_completion, _parse_tool_call
from dynamic_firm.runtime.models import ModelRequest, ModelResponse, ToolSchema, Usage, to_primitive
from dynamic_firm.runtime.ports import CancellationToken, ModelProviderError, OperationCancelled


@dataclass(frozen=True, slots=True)
class ExternalExecProviderConfig:
    workspace: Path
    command: str
    model: str
    timeout_seconds: float = 120.0
    max_request_bytes: int = 1_000_000
    max_response_bytes: int = 1_000_000
    max_stderr_bytes: int = 64_000


class ExternalExecProvider:
    """A private JSON stdin/stdout adapter, not a generic shell or plugin host."""

    def __init__(self, config: ExternalExecProviderConfig, *, environ: Mapping[str, str] | None = None) -> None:
        self.config = config
        self.workspace = config.workspace.expanduser().resolve()
        self._environ = dict(os.environ if environ is None else environ)
        self.executable = self.resolve_executable(config.command, environ=self._environ)
        self._validate()

    @staticmethod
    def resolve_executable(command: str, *, environ: Mapping[str, str] | None = None) -> str | None:
        candidate = command.strip()
        if not candidate or "\x00" in candidate or any(char.isspace() for char in candidate):
            return None
        path = Path(candidate).expanduser()
        if path.is_absolute():
            resolved = path.resolve()
            return str(resolved) if resolved.is_file() and os.access(resolved, os.X_OK) else None
        if path.parent != Path("."):
            return None
        return shutil.which(candidate, path=(environ or os.environ).get("PATH"))

    @staticmethod
    def _child_environment(source: Mapping[str, str]) -> dict[str, str]:
        # OAuth-capable CLIs commonly keep an encrypted/session credential in
        # their own user-owned store.  Keep only platform lookup variables;
        # never forward Noruct/provider credential variables into this bridge.
        names = {"HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMP", "TMPDIR", "TEMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "SYSTEMROOT", "PATHEXT", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        return {name: value for name, value in source.items() if name in names and isinstance(value, str)}

    def _validate(self) -> None:
        if not self.workspace.is_dir():
            raise ValueError(f"External provider workspace is not a directory: {self.workspace}")
        if self.executable is None:
            raise ValueError(f"External provider executable was not found: {self.config.command}")
        if not self.config.model.strip():
            raise ValueError("External provider model must be non-empty")
        if any(value <= 0 for value in (self.config.timeout_seconds, self.config.max_request_bytes, self.config.max_response_bytes, self.config.max_stderr_bytes)):
            raise ValueError("External provider limits must be positive")

    async def complete(self, request: ModelRequest, cancellation: CancellationToken) -> ModelResponse:
        cancellation.raise_if_cancelled()
        if not request.messages or request.call_index < 1:
            raise ModelProviderError("MODEL_REQUEST_INVALID", "External provider request is missing required fields.", retryable=False)
        payload = {
            "schema": "noruct.external-model-exec.v1",
            "request_id": f"{request.run_id}:{request.call_index}",
            "run_id": request.run_id,
            "call_index": request.call_index,
            "model": self.config.model,
            "messages": [to_primitive(item) for item in request.messages],
            "tools": [self._tool_schema(item) for item in request.tools],
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ModelProviderError("MODEL_REQUEST_INVALID", "External provider request could not be encoded.", retryable=False) from exc
        if len(encoded) > self.config.max_request_bytes:
            raise ModelProviderError("MODEL_REQUEST_TOO_LARGE", "External provider request exceeded the configured byte limit.", retryable=False)
        result = await self._run(encoded, cancellation)
        return self._parse_result(result, request.tools)

    @staticmethod
    def _tool_schema(value: ToolSchema) -> Mapping[str, object]:
        return {"name": value.name, "description": value.description, "input_schema": value.input_schema}

    async def _run(self, request: bytes, cancellation: CancellationToken) -> Mapping[str, Any]:
        assert self.executable is not None
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                cwd=self.workspace,
                env=self._child_environment(self._environ),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise ModelProviderError("MODEL_TRANSPORT_ERROR", "External provider process could not be started.", retryable=True) from exc
        io_task = asyncio.create_task(self._communicate(process, request))
        cancel_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait({io_task, cancel_task}, timeout=self.config.timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done:
                await self._terminate(process)
                io_task.cancel()
                await asyncio.gather(io_task, return_exceptions=True)
                raise OperationCancelled(cancellation.reason or "External provider cancelled")
            if io_task not in done:
                await self._terminate(process)
                io_task.cancel()
                await asyncio.gather(io_task, return_exceptions=True)
                raise ModelProviderError("MODEL_TIMEOUT", "External provider request timed out.", retryable=True)
            returncode, stdout, _stderr = await io_task
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            if process.returncode is None:
                await self._terminate(process)
        if returncode != 0:
            raise ModelProviderError("MODEL_UPSTREAM_ERROR", "External provider process exited without a result.", retryable=True)
        try:
            value = json.loads(stdout.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelProviderError("MODEL_STRUCTURED_OUTPUT_INVALID", "External provider did not return one UTF-8 JSON response.", retryable=False) from exc
        if not isinstance(value, Mapping):
            raise ModelProviderError("MODEL_STRUCTURED_OUTPUT_INVALID", "External provider response must be a JSON object.", retryable=False)
        return value

    async def _communicate(self, process: asyncio.subprocess.Process, request: bytes) -> tuple[int, bytes, bytes]:
        assert process.stdin is not None
        process.stdin.write(request)
        await process.stdin.drain()
        process.stdin.close()
        try:
            await process.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
        stdout_task = asyncio.create_task(self._read_bounded(process.stdout, self.config.max_response_bytes))
        stderr_task = asyncio.create_task(self._read_bounded(process.stderr, self.config.max_stderr_bytes))
        try:
            returncode = await process.wait()
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            return returncode, stdout, stderr
        except BaseException:
            for task in (stdout_task, stderr_task):
                task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise

    @staticmethod
    async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> bytes:
        if stream is None:
            return b""
        data = await stream.read(limit + 1)
        if len(data) > limit:
            raise ModelProviderError("MODEL_RESPONSE_TOO_LARGE", "External provider output exceeded the configured byte limit.", retryable=False)
        return data

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if os.name == "posix" and process.pid:
            # start_new_session makes the process leader's PID its process
            # group ID.  The leader can exit promptly after SIGTERM while a
            # child ignores it and keeps inherited pipes open, so always
            # finish the group escalation even when ``process.wait()`` has
            # already reaped the leader.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except ProcessLookupError:
                pass
            except TimeoutError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            return
        if process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=1)
        except (ProcessLookupError, TimeoutError):
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()

    @staticmethod
    def _parse_result(value: Mapping[str, Any], tools: tuple[ToolSchema, ...]) -> ModelResponse:
        # A response is exactly one of completion or one parent-owned tool
        # call. Tool calls cannot smuggle executable code outside ToolExecutor.
        kind = value.get("kind")
        if kind == "tool_call":
            raw = value.get("tool_call")
            try:
                call = _parse_tool_call(raw)
            except Exception as exc:
                raise ModelProviderError("MODEL_STRUCTURED_OUTPUT_INVALID", "External provider returned an invalid tool call.", retryable=False) from exc
            if call.name not in {tool.name for tool in tools}:
                raise ModelProviderError("MODEL_STRUCTURED_OUTPUT_INVALID", "External provider requested a tool outside the parent-owned contract.", retryable=False)
            return ModelResponse(tool_calls=(call,), usage=Usage(), finish_reason="tool_calls")
        if kind != "completion" or not isinstance(value.get("completion"), Mapping):
            raise ModelProviderError("MODEL_STRUCTURED_OUTPUT_INVALID", "External provider response must be a completion or one tool call.", retryable=False)
        completion_raw = json.dumps(value["completion"], ensure_ascii=False, sort_keys=True)
        completion = _parse_completion(completion_raw)
        if completion is None:
            raise ModelProviderError("MODEL_STRUCTURED_OUTPUT_INVALID", "External provider returned an invalid completion envelope.", retryable=False)
        return ModelResponse(content=completion_raw, completion=completion, usage=Usage(), finish_reason="stop")
