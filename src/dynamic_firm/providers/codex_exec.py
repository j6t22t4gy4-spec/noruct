from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from dynamic_firm.coding.models import CodingWorkRequest, CodingWorkResult
from dynamic_firm.coding.ports import CodingWorkerError, CodingWorkerPort
from dynamic_firm.providers.openai_compat import (
    _completion_response_format,
    _parse_completion,
    _parse_tool_call,
)
from dynamic_firm.providers.wire_safety import sanitize_wire_payload
from dynamic_firm.runtime.models import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    StructuredOutputRequest,
    StructuredOutputResponse,
    Usage,
    to_primitive,
)
from dynamic_firm.runtime.ports import (
    CancellationToken,
    ModelProviderError,
    OperationCancelled,
)
from dynamic_firm.runtime.redaction import redact_prompt_text


_SAFE_ENVIRONMENT_KEYS = {
    "APPDATA",
    "CODEX_HOME",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
}
_SAFE_VALIDATION_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SAFE_VALIDATION_DETAIL = re.compile(r"[A-Za-z0-9_.:,;=+() -]{0,512}")
_CODEX_DEFAULT_MODEL_SELECTOR = "codex-default"


def _codex_model_argument(model: str | None) -> str | None:
    """Return an explicit Codex model only when Noruct has one to select.

    ``codex-default`` is Noruct's persisted selector for "use the authenticated
    Codex CLI default".  It is not a public model identifier and must never be
    forwarded as ``codex exec --model codex-default``.  Omitting the flag keeps
    the selection with the user's authenticated Codex installation.
    """

    if model is None:
        return None
    selected = model.strip()
    return None if not selected or selected == _CODEX_DEFAULT_MODEL_SELECTOR else selected


def _is_unsupported_model_error(stderr_lower: str) -> bool:
    """Classify only stable Codex CLI model-selection diagnostics.

    Stderr itself is never exposed to callers.  A bad local model selector is
    deterministic and non-retryable, unlike a transient upstream failure.
    """

    return any(
        marker in stderr_lower
        for marker in (
            "unknown model",
            "model is not supported",
            "invalid model",
        )
    )


@dataclass(frozen=True, slots=True)
class CodexExecProviderConfig:
    workspace: Path
    command: str = "codex"
    model: str | None = None
    # This is a leak guard for a genuinely hung child process, not an
    # interactive responsiveness deadline.  A healthy ``codex exec --json``
    # turn may legitimately take a long time while it keeps emitting events.
    timeout_seconds: float = 1_800.0
    # A separate liveness deadline gives the operator a fast, retryable result
    # when the backend is silent.  It is reset by every JSONL protocol record.
    stale_timeout_seconds: float = 90.0
    max_prompt_bytes: int = 1_000_000
    max_event_bytes: int = 1_000_000
    max_stderr_bytes: int = 64_000
    max_result_bytes: int = 1_000_000


@dataclass(frozen=True, slots=True)
class CodexLoginStatus:
    executable: str | None
    installed: bool
    authenticated: bool


class _OutputLimitExceeded(Exception):
    pass


def _response_schema(tools: tuple[ToolSchema, ...]) -> dict[str, Any]:
    """Use an unambiguous parent-tool envelope when tools are available.

    ``codex exec`` accepts an output schema rather than a native function-call
    transport.  The envelope therefore carries two legal turn shapes: a
    tool-intent turn contains one parent-owned call, while a final completion
    turn keeps all tool fields empty. The CLI's supported schema subset cannot
    encode that cross-field condition, so the parent parser verifies it after
    decoding. Without the distinction a model can return a completion-shaped
    answer with a copied tool name, which looks superficially valid but cannot
    be executed by the parent ledger.
    """

    completion = _completion_response_format()["json_schema"]["schema"]
    if not tools:
        return completion
    properties = dict(completion["properties"])
    properties.update(
        {
            "kind": {"type": "string", "enum": ["completion", "tool_call"]},
            "tool_call_id": {
                "type": "string",
                "description": (
                    "For kind=tool_call, a non-empty opaque call identifier. "
                    "For kind=completion, an empty string."
                ),
            },
            "tool_name": {
                "type": "string",
                "enum": ["", *[tool.name for tool in tools]],
                "description": (
                    "For kind=tool_call, one listed parent tool name. For "
                    "kind=completion, an empty string."
                ),
            },
            "tool_arguments_json": {
                "type": "string",
                "description": (
                    "For kind=tool_call, a JSON object encoded as text. For "
                    "kind=completion, use an empty string."
                ),
            },
        }
    )
    return {
        "type": "object",
        "properties": properties,
        "required": [
            *completion["required"],
            "kind",
            "tool_call_id",
            "tool_name",
            "tool_arguments_json",
        ],
        "additionalProperties": False,
    }


def _parse_employee_response(
    value: Mapping[str, Any],
    tools: tuple[ToolSchema, ...],
) -> ModelResponse:
    """Normalize a constrained Codex result to the existing parent tool loop."""

    if tools and value.get("kind") == "tool_call":
        try:
            call = _parse_tool_call(
                {
                    "id": value.get("tool_call_id"),
                    "function": {
                        "name": value.get("tool_name"),
                        "arguments": value.get("tool_arguments_json"),
                    },
                }
            )
        except ModelProviderError:
            raise
        except Exception:
            raise ModelProviderError(
                "MODEL_STRUCTURED_OUTPUT_INVALID",
                "Codex returned an invalid parent tool call.",
                retryable=False,
            ) from None
        allowed = {tool.name for tool in tools}
        if call.name not in allowed:
            raise ModelProviderError(
                "MODEL_STRUCTURED_OUTPUT_INVALID",
                "Codex requested a tool outside the parent-owned contract.",
                retryable=False,
            )
        return ModelResponse(tool_calls=(call,))
    if tools and value.get("kind") != "completion":
        raise ModelProviderError(
            "MODEL_STRUCTURED_OUTPUT_INVALID",
            "Codex omitted the required parent response kind.",
            retryable=False,
        )
    if tools:
        # Validate the branch a second time.  The external CLI normally
        # enforces the schema, but this defensive check keeps a malformed or
        # older CLI result from being reinterpreted as a normal completion.
        if any(
            value.get(field) not in (None, "")
            for field in ("tool_call_id", "tool_name", "tool_arguments_json")
        ):
            raise ModelProviderError(
                "MODEL_STRUCTURED_OUTPUT_INVALID",
                "Codex mixed parent tool fields into an employee completion.",
                retryable=False,
            )
    completion = _parse_completion(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    if completion is None:
        raise ModelProviderError(
            "MODEL_STRUCTURED_OUTPUT_INVALID",
            "Codex returned an invalid employee completion.",
            retryable=False,
        )
    return ModelResponse(
        content=json.dumps(value, ensure_ascii=False, sort_keys=True),
        completion=completion,
    )


class CodexExecProvider:
    """Read-only adapter over the official, user-authenticated Codex CLI surface.

    Codex is an agent runtime rather than a raw completion transport. This adapter
    deliberately exposes only schema-constrained final results to the Native Runtime.
    """

    def __init__(
        self,
        config: CodexExecProviderConfig,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._environ = dict(os.environ if environ is None else environ)
        self.workspace = config.workspace.expanduser().resolve()
        self.executable = self.resolve_executable(config.command, environ=self._environ)
        self._cancelled_request_ids: dict[str, str] = {}
        self._validate()

    def consume_cancelled_request_id(self, run_id: str) -> str | None:
        """Return a request identity observed before a cancelled subprocess ended."""

        return self._cancelled_request_ids.pop(run_id, None)

    def observed_request_id(self, run_id: str) -> str | None:
        """Expose the already-redacted request identity for cancellation coordination."""

        return self._cancelled_request_ids.get(run_id)

    def _validate(self) -> None:
        if not self.workspace.is_dir():
            raise ValueError(f"Codex workspace is not a directory: {self.workspace}")
        if self.executable is None:
            raise ValueError(
                f"Codex executable was not found: {self.config.command}. "
                "Install Codex CLI or configure an absolute codex_command path."
            )
        if self.config.model is not None and not self.config.model.strip():
            raise ValueError("Codex model must be omitted or non-empty")
        numeric_limits = (
            self.config.timeout_seconds,
            self.config.max_prompt_bytes,
            self.config.max_event_bytes,
            self.config.max_stderr_bytes,
            self.config.max_result_bytes,
        )
        if any(value <= 0 for value in numeric_limits):
            raise ValueError("Codex provider limits must be positive")

    @staticmethod
    def resolve_executable(
        command: str,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> str | None:
        candidate = command.strip()
        if not candidate or "\x00" in candidate:
            return None
        path = Path(candidate).expanduser()
        if path.is_absolute():
            resolved = path.resolve()
            return str(resolved) if resolved.is_file() and os.access(resolved, os.X_OK) else None
        if path.parent != Path("."):
            return None
        search_path = (environ or os.environ).get("PATH")
        return shutil.which(candidate, path=search_path)

    @classmethod
    def login_status(
        cls,
        command: str = "codex",
        *,
        environ: Mapping[str, str] | None = None,
        timeout_seconds: float = 10.0,
    ) -> CodexLoginStatus:
        source_environment = dict(os.environ if environ is None else environ)
        executable = cls.resolve_executable(command, environ=source_environment)
        if executable is None:
            return CodexLoginStatus(None, installed=False, authenticated=False)
        try:
            result = subprocess.run(
                [executable, "login", "status"],
                env=cls._child_environment(source_environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return CodexLoginStatus(executable, installed=True, authenticated=False)
        return CodexLoginStatus(
            executable,
            installed=True,
            authenticated=result.returncode == 0,
        )

    async def complete(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        if not request.messages or request.call_index < 1:
            raise ModelProviderError(
                "MODEL_REQUEST_INVALID",
                "Codex model request is missing required fields.",
                retryable=False,
            )
        schema = _response_schema(request.tools)
        value, usage, request_id = await self._execute(
            messages=request.messages,
            schema_name="dynamic_firm_employee_completion",
            schema=schema,
            cancellation=cancellation,
            tools=request.tools,
            run_id=request.run_id,
        )
        response = _parse_employee_response(value, request.tools)
        return ModelResponse(
            content=response.content,
            tool_calls=response.tool_calls,
            completion=response.completion,
            usage=usage,
            provider_request_id=request_id,
            finish_reason="stop",
        )

    async def complete_structured(
        self,
        request: StructuredOutputRequest,
        cancellation: CancellationToken,
    ) -> StructuredOutputResponse:
        cancellation.raise_if_cancelled()
        if not request.schema_name.strip() or not request.messages or request.call_index < 1:
            raise ModelProviderError(
                "MODEL_REQUEST_INVALID",
                "Structured Codex request is missing required fields.",
                retryable=False,
            )
        value, usage, request_id = await self._execute(
            messages=request.messages,
            schema_name=request.schema_name,
            schema=request.json_schema,
            cancellation=cancellation,
            tools=(),
            run_id=request.request_id,
        )
        return StructuredOutputResponse(
            value=value,
            usage=usage,
            provider_request_id=request_id,
            finish_reason="stop",
        )

    async def _execute(
        self,
        *,
        messages: tuple[ModelMessage, ...],
        schema_name: str,
        schema: Mapping[str, Any],
        cancellation: CancellationToken,
        tools: tuple[ToolSchema, ...],
        run_id: str,
    ) -> tuple[dict[str, Any], Usage, str | None]:
        prompt = self._prompt(
            messages,
            schema_name,
            tools=tools,
        )
        encoded_prompt = prompt.encode("utf-8")
        if len(encoded_prompt) > self.config.max_prompt_bytes:
            raise ModelProviderError(
                "MODEL_REQUEST_TOO_LARGE",
                "Codex request exceeded the configured prompt byte limit.",
                retryable=False,
            )

        with tempfile.TemporaryDirectory(prefix="noruct-codex-") as temporary:
            root = Path(temporary)
            schema_path = root / "output-schema.json"
            result_path = root / "final-result.json"
            try:
                schema_payload = dict(schema)
                sanitize_wire_payload(schema_payload)
                schema_path.write_text(
                    json.dumps(schema_payload, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                os.chmod(schema_path, 0o600)
            except (OSError, TypeError, ValueError):
                raise ModelProviderError(
                    "MODEL_REQUEST_INVALID",
                    "Codex output schema could not be encoded safely.",
                    retryable=False,
                ) from None

            command = self._exec_command(schema_path=schema_path, result_path=result_path)
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=self.workspace,
                    env=self._child_environment(self._environ),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=(os.name == "posix"),
                )
            except OSError:
                raise ModelProviderError(
                    "MODEL_TRANSPORT_ERROR",
                    "Codex process could not be started.",
                    retryable=True,
                ) from None

            try:
                returncode, stdout, stderr = await self._communicate(
                    process,
                    encoded_prompt,
                    cancellation,
                    request_id_sink=lambda value: self._record_request_id(
                        run_id,
                        value,
                    ),
                )
            except _OutputLimitExceeded:
                raise ModelProviderError(
                    "MODEL_RESPONSE_TOO_LARGE",
                    "Codex process output exceeded the configured byte limit.",
                    retryable=False,
                ) from None
            except BaseException:
                if not cancellation.cancelled:
                    self._cancelled_request_ids.pop(run_id, None)
                raise

            if returncode != 0:
                lowered = stderr.decode("utf-8", errors="replace").lower()
                if "not logged in" in lowered or "login" in lowered and "required" in lowered:
                    raise ModelProviderError(
                        "MODEL_AUTH_FAILED",
                        "Codex is not authenticated. Run `codex login` or sign in through the Codex IDE extension.",
                        retryable=False,
                    )
                if _is_unsupported_model_error(lowered):
                    raise ModelProviderError(
                        "MODEL_CONFIGURATION_INVALID",
                        "The configured Codex model is not supported by this authenticated Codex installation.",
                        retryable=False,
                    )
                raise ModelProviderError(
                    "MODEL_UPSTREAM_ERROR",
                    "Codex execution failed before returning a valid result.",
                    retryable=True,
                )

            value = self._read_result(result_path)
            usage, request_id = self._parse_events(stdout)
            self._cancelled_request_ids.pop(run_id, None)
            return value, usage, request_id

    def _record_request_id(self, run_id: str, request_id: str) -> None:
        if run_id and request_id:
            self._cancelled_request_ids[run_id] = request_id

    def _exec_command(self, *, schema_path: Path, result_path: Path) -> list[str]:
        assert self.executable is not None
        command = [
            self.executable,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--disable",
            "multi_agent",
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "browser_use",
            "--disable",
            "computer_use",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "-c",
            'web_search="disabled"',
            "-c",
            'shell_environment_policy.inherit="none"',
            "-C",
            str(self.workspace),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
        ]
        if model := _codex_model_argument(self.config.model):
            command.extend(("--model", model))
        command.append("-")
        return command

    async def _communicate(
        self,
        process: asyncio.subprocess.Process,
        prompt: bytes,
        cancellation: CancellationToken,
        *,
        request_id_sink: Callable[[str], None] | None = None,
    ) -> tuple[int, bytes, bytes]:
        if self.config.timeout_seconds <= 0 or self.config.stale_timeout_seconds <= 0:
            raise ValueError("Codex hard and no-progress timeouts must be positive")

        loop = asyncio.get_running_loop()
        last_progress_at = loop.time()

        def observe_progress() -> None:
            nonlocal last_progress_at
            last_progress_at = loop.time()

        async def run_io() -> tuple[int, bytes, bytes]:
            stdout_reader = asyncio.create_task(
                self._read_events_bounded(
                    process.stdout,
                    self.config.max_event_bytes,
                    request_id_sink=request_id_sink,
                    progress_sink=observe_progress,
                ),
                name="codex-exec-stdout",
            )
            stderr_reader = asyncio.create_task(
                self._read_bounded(process.stderr, self.config.max_stderr_bytes),
                name="codex-exec-stderr",
            )
            waiter = asyncio.create_task(process.wait(), name="codex-exec-process")
            try:
                assert process.stdin is not None
                process.stdin.write(prompt)
                await process.stdin.drain()
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                returncode, stdout, stderr = await asyncio.gather(
                    waiter,
                    stdout_reader,
                    stderr_reader,
                )
                return returncode, stdout, stderr
            except BaseException:
                await self._terminate_process(process)
                for task in (waiter, stdout_reader, stderr_reader):
                    task.cancel()
                await asyncio.gather(
                    waiter,
                    stdout_reader,
                    stderr_reader,
                    return_exceptions=True,
                )
                raise

        io_task = asyncio.create_task(run_io(), name="codex-exec-io")
        cancel_task = asyncio.create_task(cancellation.wait(), name="codex-exec-cancel")
        hard_deadline = loop.time() + self.config.timeout_seconds

        async def abort_io() -> None:
            await self._terminate_process(process)
            if not io_task.done():
                io_task.cancel()
            await asyncio.gather(io_task, return_exceptions=True)

        try:
            while True:
                now = loop.time()
                hard_remaining = hard_deadline - now
                stale_remaining = self.config.stale_timeout_seconds - (now - last_progress_at)
                done, _ = await asyncio.wait(
                    {io_task, cancel_task},
                    timeout=max(0.0, min(hard_remaining, stale_remaining)),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done:
                    await abort_io()
                    raise OperationCancelled(cancellation.reason or "Codex execution cancelled")
                if io_task in done:
                    try:
                        return await io_task
                    except _OutputLimitExceeded:
                        await self._terminate_process(process)
                        raise

                # A JSONL event may have landed exactly as the wait elapsed;
                # re-evaluate both clocks before declaring a failure.
                now = loop.time()
                if now >= hard_deadline:
                    await abort_io()
                    raise ModelProviderError(
                        "MODEL_TIMEOUT",
                        "Codex exceeded the hard provider guard.",
                        retryable=True,
                    )
                if now - last_progress_at >= self.config.stale_timeout_seconds:
                    await abort_io()
                    raise ModelProviderError(
                        "MODEL_STALE",
                        "Codex produced no progress events before the configured no-progress deadline.",
                        retryable=True,
                    )
        except asyncio.CancelledError:
            await abort_io()
            raise
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    @staticmethod
    async def _read_bounded(
        stream: asyncio.StreamReader | None,
        limit: int,
    ) -> bytes:
        if stream is None:
            return b""
        collected = bytearray()
        exceeded = False
        while True:
            chunk = await stream.read(8_192)
            if not chunk:
                if exceeded:
                    raise _OutputLimitExceeded
                return bytes(collected)
            remaining = max(0, limit - len(collected))
            if remaining:
                collected.extend(chunk[:remaining])
            if len(chunk) > remaining:
                exceeded = True

    @staticmethod
    async def _read_events_bounded(
        stream: asyncio.StreamReader | None,
        limit: int,
        *,
        request_id_sink: Callable[[str], None] | None,
        progress_sink: Callable[[], None] | None = None,
    ) -> bytes:
        """Collect bounded JSONL while exposing only a safe thread identity early."""

        if stream is None:
            return b""
        collected = bytearray()
        exceeded = False
        while True:
            # Codex emits JSONL while a turn is still running.  `read(n)` may
            # remain pending until EOF on a quiet pipe, which loses the early
            # thread identity needed to attach a subsequent cancellation to the
            # right external request.  A line is the protocol record boundary.
            line = await stream.readline()
            if not line:
                if exceeded:
                    raise _OutputLimitExceeded
                return bytes(collected)
            remaining = max(0, limit - len(collected))
            if remaining:
                collected.extend(line[:remaining])
            if len(line) > remaining:
                exceeded = True
            if progress_sink is not None:
                progress_sink()
            CodexExecProvider._observe_request_id(line, request_id_sink)

    @staticmethod
    def _observe_request_id(
        line: bytes | bytearray,
        request_id_sink: Callable[[str], None] | None,
    ) -> None:
        if request_id_sink is None or not line.strip():
            return
        try:
            event = json.loads(bytes(line).decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(event, dict):
            return
        request_id = event.get("thread_id")
        if event.get("type") == "thread.started" and isinstance(request_id, str):
            request_id_sink(request_id)

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if os.name == "posix" and process.pid:
            # The CLI is the leader of a private session.  Reaping only that
            # leader after SIGTERM is insufficient: a child that ignores
            # SIGTERM can retain the group's pipes and survive the request.
            # Complete the SIGKILL escalation for the owned process group
            # after the leader wait as well.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
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
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
            return
        except TimeoutError:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()

    def _read_result(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("rb") as handle:
                raw = handle.read(self.config.max_result_bytes + 1)
        except OSError:
            raise ModelProviderError(
                "MODEL_RESPONSE_INVALID",
                "Codex did not produce the required final result.",
                retryable=True,
            ) from None
        if len(raw) > self.config.max_result_bytes:
            raise ModelProviderError(
                "MODEL_RESPONSE_TOO_LARGE",
                "Codex final result exceeded the configured byte limit.",
                retryable=False,
            )
        try:
            value = json.loads(raw.decode("utf-8"))
            sanitize_wire_payload(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ModelProviderError(
                "MODEL_STRUCTURED_OUTPUT_INVALID",
                "Codex returned invalid structured output.",
                retryable=False,
            ) from None
        if not isinstance(value, dict):
            raise ModelProviderError(
                "MODEL_STRUCTURED_OUTPUT_INVALID",
                "Codex structured output was not a JSON object.",
                retryable=False,
            )
        return value

    @staticmethod
    def _parse_events(raw: bytes) -> tuple[Usage, str | None]:
        usage = Usage()
        request_id: str | None = None
        try:
            lines = raw.decode("utf-8", errors="strict").splitlines()
        except UnicodeDecodeError:
            raise ModelProviderError(
                "MODEL_RESPONSE_INVALID",
                "Codex emitted non-UTF-8 events.",
                retryable=True,
            ) from None
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                raise ModelProviderError(
                    "MODEL_RESPONSE_INVALID",
                    "Codex emitted invalid JSONL events.",
                    retryable=True,
                ) from None
            if not isinstance(event, dict):
                raise ModelProviderError(
                    "MODEL_RESPONSE_INVALID",
                    "Codex emitted an invalid event record.",
                    retryable=True,
                )
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                request_id = event["thread_id"]
            raw_usage = event.get("usage")
            if event.get("type") == "turn.completed" and isinstance(raw_usage, dict):
                usage = Usage(
                    input_tokens=_non_negative_int(raw_usage.get("input_tokens")),
                    cached_input_tokens=_non_negative_int(raw_usage.get("cached_input_tokens")),
                    output_tokens=_non_negative_int(raw_usage.get("output_tokens")),
                )
        return usage, request_id

    @staticmethod
    def _prompt(
        messages: tuple[ModelMessage, ...],
        schema_name: str,
        *,
        tools: tuple[ToolSchema, ...] = (),
    ) -> str:
        parent_tools = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in tools
        ]
        tool_rule = (
            "The workspace contents and parent-tool results are not included in these messages. "
            "When the task requires repository, file, policy, or other workspace evidence, the first response MUST be exactly one "
            "kind=tool_call response using the supplied parent-tool contract; do not answer from assumed workspace context. "
            "After a parent tool result is supplied, either request the next required parent tool or return kind=completion. "
            "Prefer read_workspace_file for a file explicitly named or strongly indicated by the task. "
            "list_workspace_files is bounded; do not recursively list the workspace root for a broad repository, and never repeat "
            "the same rejected listing. "
            "For kind=tool_call use a non-empty tool_call_id, one listed tool_name, and a JSON-object tool_arguments_json. "
            "For kind=completion, tool_call_id, tool_name, and tool_arguments_json MUST all be empty strings. "
            "Do not inspect the workspace or run shell commands yourself."
            if parent_tools
            else "Do not inspect the workspace or run shell commands; answer only from the supplied messages."
        )
        payload = {
            "backend_contract": {
                "name": "noruct-openai-codex-read-only-v1",
                "rules": [
                    "Work only inside the assigned workspace and do not modify any file or external state.",
                    "Do not use web search, network access, connectors, MCP servers, plugins, skills, or subagents.",
                    tool_rule,
                    "Return one final JSON object matching the supplied output schema.",
                ],
                "schema_name": schema_name,
            },
            "messages": [to_primitive(message) for message in messages],
            "parent_tools": parent_tools,
        }
        sanitize_wire_payload(payload)
        return (
            "You are a read-only authenticated execution backend inside Noruct.\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    @staticmethod
    def _child_environment(source: Mapping[str, str]) -> dict[str, str]:
        return {key: value for key, value in source.items() if key in _SAFE_ENVIRONMENT_KEYS}


from .codex_coding_worker import CodexExecCodingWorker  # noqa: E402


def _non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
